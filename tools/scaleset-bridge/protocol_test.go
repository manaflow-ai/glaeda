package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/actions/scaleset"
)

type fakeBackend struct {
	initial       *scaleset.RunnerScaleSetStatistic
	message       *scaleset.RunnerScaleSetMessage
	jit           *scaleset.RunnerScaleSetJitRunnerConfig
	runner        *scaleset.RunnerReference
	acquired      []int64
	calls         []string
	lastMessageID int
	maxCapacity   int
	validation    []error
}

func (backend *fakeBackend) Poll(_ context.Context, lastMessageID, maxCapacity int) (*scaleset.RunnerScaleSetMessage, error) {
	backend.calls = append(backend.calls, "poll")
	backend.lastMessageID = lastMessageID
	backend.maxCapacity = maxCapacity
	message := backend.message
	backend.message = nil
	return message, nil
}
func (backend *fakeBackend) DeleteMessage(_ context.Context, messageID int) error {
	backend.calls = append(backend.calls, fmt.Sprintf("delete:%d", messageID))
	return nil
}
func (backend *fakeBackend) AcquireJobs(_ context.Context, requestIDs []int64) ([]int64, error) {
	backend.calls = append(backend.calls, fmt.Sprintf("acquire:%v", requestIDs))
	return append([]int64(nil), backend.acquired...), nil
}
func (backend *fakeBackend) GenerateJIT(_ context.Context, name, workFolder string) (*scaleset.RunnerScaleSetJitRunnerConfig, error) {
	backend.calls = append(backend.calls, fmt.Sprintf("jit:%s:%s", name, workFolder))
	return backend.jit, nil
}
func (backend *fakeBackend) GetRunner(_ context.Context, id int) (*scaleset.RunnerReference, error) {
	backend.calls = append(backend.calls, fmt.Sprintf("runner-id:%d", id))
	return backend.runner, nil
}
func (backend *fakeBackend) GetRunnerByName(_ context.Context, name string) (*scaleset.RunnerReference, error) {
	backend.calls = append(backend.calls, "runner:"+name)
	return backend.runner, nil
}
func (backend *fakeBackend) RemoveRunner(_ context.Context, id int64) error {
	backend.calls = append(backend.calls, fmt.Sprintf("remove:%d", id))
	return nil
}
func (backend *fakeBackend) ValidateScaleSet(context.Context) error {
	backend.calls = append(backend.calls, "validate-set")
	if len(backend.validation) == 0 {
		return nil
	}
	err := backend.validation[0]
	backend.validation = backend.validation[1:]
	return err
}
func (backend *fakeBackend) InitialStatistics() *scaleset.RunnerScaleSetStatistic {
	return backend.initial
}
func (backend *fakeBackend) Close(context.Context) error {
	backend.calls = append(backend.calls, "close")
	return nil
}

func validStart() startConfig {
	return startConfig{
		GitHubConfigURL: "https://github.com/example/project",
		ClientID:        "Iv1.example",
		InstallationID:  17,
		PrivateKey:      "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----",
		ScaleSetID:      23,
		ScaleSetName:    "smolrunner",
		RunnerGroupID:   1,
		Labels:          []string{"smolrunner"},
		Owner:           "smolrunner-host",
		MaxCapacity:     1,
	}
}

func startedServer(t *testing.T, fake *fakeBackend) *server {
	t.Helper()
	if fake.initial == nil {
		fake.initial = &scaleset.RunnerScaleSetStatistic{}
	}
	server := newServer(func(context.Context, startConfig) (backend, error) {
		return fake, nil
	})
	response := server.handle(context.Background(), protocolRequest{
		Version:   protocolVersion,
		Operation: "start",
		Start:     validStart(),
	})
	if response.Type != "ready" {
		t.Fatalf("start response = %#v", response)
	}
	return server
}

func TestDecodeRequestIsStrictAndVersioned(t *testing.T) {
	valid := []byte(`{"version":3,"operation":"poll"}`)
	request, err := decodeRequest(valid)
	if err != nil || request.Operation != "poll" {
		t.Fatalf("valid request: request=%#v err=%v", request, err)
	}
	for _, input := range [][]byte{
		[]byte(`{"version":1,"operation":"poll"}`),
		[]byte(`{"version":2,"operation":"poll"}`),
		[]byte(`{"version":3,"operation":"poll","unknown":true}`),
		[]byte(`{"version":3,"operation":"poll"} {}`),
	} {
		if _, err := decodeRequest(input); err == nil {
			t.Fatalf("accepted invalid request %s", input)
		}
	}
}

func TestPollRequiresDurableAckBeforeAdvancing(t *testing.T) {
	backend := &fakeBackend{
		initial:  &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 1},
		acquired: []int64{41},
		message: &scaleset.RunnerScaleSetMessage{
			MessageID: 7,
			Statistics: &scaleset.RunnerScaleSetStatistic{
				TotalAvailableJobs: 1,
				TotalAssignedJobs:  1,
			},
			JobAvailableMessages: []*scaleset.JobAvailable{{
				JobMessageBase: scaleset.JobMessageBase{
					RunnerRequestID: 41,
					RepositoryName:  "project",
					OwnerName:       "example",
					JobID:           "job-1",
					WorkflowRunID:   99,
					RequestLabels:   []string{"smolrunner"},
				},
			}},
		},
	}
	server := startedServer(t, backend)

	message := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1})
	if message.Type != "message" || message.MessageID != 7 || len(message.Events) != 1 {
		t.Fatalf("message response = %#v", message)
	}
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1}); response.Code != "ack_required" {
		t.Fatalf("second poll response = %#v", response)
	}
	if !reflect.DeepEqual(backend.calls, []string{"poll"}) {
		t.Fatalf("calls before ack = %v", backend.calls)
	}

	acked := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "ack", MessageID: 7})
	if acked.Type != "acked" || !reflect.DeepEqual(acked.AcquiredRequests, []int64{41}) {
		t.Fatalf("ack response = %#v", acked)
	}
	if !reflect.DeepEqual(backend.calls, []string{"poll", "delete:7", "acquire:[41]"}) {
		t.Fatalf("ack ordering = %v", backend.calls)
	}
	if server.lastAckedID != 7 || server.pending != nil || backend.maxCapacity != 1 {
		t.Fatalf("server did not advance exact ack state")
	}
}

func TestIdleRetainsLatestValidatedStatistics(t *testing.T) {
	backend := &fakeBackend{
		initial: &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 8},
		message: &scaleset.RunnerScaleSetMessage{
			MessageID:  7,
			Statistics: &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 3},
		},
	}
	server := startedServer(t, backend)
	message := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1})
	if message.Type != "message" || message.Statistics == nil || message.Statistics.AssignedJobs != 3 {
		t.Fatalf("message response=%#v", message)
	}
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "ack", MessageID: 7}); response.Type != "acked" {
		t.Fatalf("ack response=%#v", response)
	}
	idle := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1})
	if idle.Type != "idle" || idle.Statistics == nil || idle.Statistics.AssignedJobs != 3 {
		t.Fatalf("idle reverted to startup statistics: %#v", idle)
	}
}

func TestResumeRestoresDurableCursorBeforeZeroCapacityLifecyclePoll(t *testing.T) {
	backend := &fakeBackend{message: &scaleset.RunnerScaleSetMessage{
		MessageID:  8,
		Statistics: &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 1, TotalRunningJobs: 1},
		JobStartedMessages: []*scaleset.JobStarted{{
			RunnerID:   81,
			RunnerName: "smolrunner-job-1",
			JobMessageBase: scaleset.JobMessageBase{
				RunnerRequestID: 41,
				RepositoryName:  "project",
				OwnerName:       "example",
				JobID:           "job-1",
				WorkflowRunID:   99,
				RequestLabels:   []string{"smolrunner"},
			},
		}},
	}}
	server := startedServer(t, backend)
	restored := server.handle(context.Background(), protocolRequest{
		Version:     protocolVersion,
		Operation:   "resume",
		LastAckedID: 7,
	})
	if restored.Type != "restored" || restored.MessageID != 7 || server.lastAckedID != 7 || !server.cursorSet {
		t.Fatalf("resume response=%#v server=%#v", restored, server)
	}
	message := server.handle(context.Background(), protocolRequest{
		Version:     protocolVersion,
		Operation:   "poll",
		MaxCapacity: 0,
	})
	if message.Type != "message" || message.MessageID != 8 || len(message.Events) != 1 || backend.maxCapacity != 0 {
		t.Fatalf("zero-capacity lifecycle poll=%#v max_capacity=%d", message, backend.maxCapacity)
	}
}

func TestResumeAndZeroCapacityPollFailClosed(t *testing.T) {
	backend := &fakeBackend{message: &scaleset.RunnerScaleSetMessage{
		MessageID:  8,
		Statistics: &scaleset.RunnerScaleSetStatistic{TotalAvailableJobs: 1},
		JobAvailableMessages: []*scaleset.JobAvailable{{JobMessageBase: scaleset.JobMessageBase{
			RunnerRequestID: 41,
			RepositoryName:  "project",
			OwnerName:       "example",
			JobID:           "job-1",
			WorkflowRunID:   99,
			RequestLabels:   []string{"smolrunner"},
		}}},
	}}
	server := startedServer(t, backend)
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "resume", LastAckedID: 7}); response.Type != "restored" {
		t.Fatalf("resume response=%#v", response)
	}
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "resume", LastAckedID: 7}); response.Code != "invalid_recovery" {
		t.Fatalf("repeated resume response=%#v", response)
	}
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 0}); response.Code != "invalid_message" || server.pending != nil {
		t.Fatalf("zero-capacity available response=%#v pending=%#v", response, server.pending)
	}

	idle := startedServer(t, &fakeBackend{})
	if response := idle.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 0}); response.Type != "idle" {
		t.Fatalf("idle response=%#v", response)
	}
	if response := idle.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "resume", LastAckedID: 7}); response.Code != "invalid_recovery" {
		t.Fatalf("post-poll resume response=%#v", response)
	}
	if response := idle.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 2}); response.Code != "invalid_capacity" {
		t.Fatalf("oversized capacity response=%#v", response)
	}
}

func TestAckRejectsForeignOrDuplicateAcquisitionWithoutRepeatingDelete(t *testing.T) {
	backend := &fakeBackend{
		acquired: []int64{42},
		message: &scaleset.RunnerScaleSetMessage{
			MessageID:  7,
			Statistics: &scaleset.RunnerScaleSetStatistic{TotalAvailableJobs: 1},
			JobAvailableMessages: []*scaleset.JobAvailable{{JobMessageBase: scaleset.JobMessageBase{
				RunnerRequestID: 41,
				RepositoryName:  "project",
				OwnerName:       "example",
				JobID:           "job-1",
				WorkflowRunID:   99,
				RequestLabels:   []string{"smolrunner"},
			}}},
		},
	}
	server := startedServer(t, backend)
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1}); response.Type != "message" {
		t.Fatalf("poll response=%#v", response)
	}
	invalid := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "ack", MessageID: 7})
	if invalid.Code != "invalid_acquisition" || server.pending == nil || !server.pending.deleted || server.lastAckedID != 0 {
		t.Fatalf("invalid acquisition advanced state: response=%#v pending=%#v", invalid, server.pending)
	}
	backend.acquired = []int64{41}
	valid := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "ack", MessageID: 7})
	if valid.Type != "acked" || !reflect.DeepEqual(valid.AcquiredRequests, []int64{41}) || !reflect.DeepEqual(backend.calls, []string{"poll", "delete:7", "acquire:[41]", "acquire:[41]"}) {
		t.Fatalf("acquisition retry response=%#v calls=%v", valid, backend.calls)
	}
}

func TestAcquiredJobIDsArePositiveUniqueSubset(t *testing.T) {
	if err := validateAcquiredJobs([]int64{41, 42}, []int64{42}); err != nil {
		t.Fatalf("valid subset refused: %v", err)
	}
	for _, acquired := range [][]int64{{0}, {43}, {41, 41}} {
		if err := validateAcquiredJobs([]int64{41, 42}, acquired); err == nil {
			t.Fatalf("invalid acquired IDs accepted: %v", acquired)
		}
	}
}

func TestPollRejectsNonAdvancingMessageID(t *testing.T) {
	backend := &fakeBackend{message: &scaleset.RunnerScaleSetMessage{MessageID: 7, Statistics: &scaleset.RunnerScaleSetStatistic{}}}
	server := startedServer(t, backend)
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1}); response.Type != "message" {
		t.Fatalf("first poll response=%#v", response)
	}
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "ack", MessageID: 7}); response.Type != "acked" {
		t.Fatalf("ack response=%#v", response)
	}
	backend.message = &scaleset.RunnerScaleSetMessage{MessageID: 7, Statistics: &scaleset.RunnerScaleSetStatistic{}}
	response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1})
	if response.Code != "invalid_message" || server.pending != nil || server.lastAckedID != 7 {
		t.Fatalf("nonadvancing message response=%#v pending=%#v", response, server.pending)
	}
}

func TestJITSecretIsReturnedOnlyForExactRunner(t *testing.T) {
	backend := &fakeBackend{
		jit: &scaleset.RunnerScaleSetJitRunnerConfig{
			Runner: &scaleset.RunnerReference{
				ID:               81,
				Name:             "smolrunner-job-1",
				RunnerScaleSetID: 23,
			},
			EncodedJITConfig: "one-time-secret",
		},
	}
	server := startedServer(t, backend)
	response := server.handle(context.Background(), protocolRequest{
		Version:    1,
		Operation:  "generate_jit",
		RunnerName: "smolrunner-job-1",
		WorkFolder: "_work",
	})
	if response.Type != "jit" || response.EncodedJITConfig != "one-time-secret" || response.Runner.ID != 81 {
		t.Fatalf("jit response = %#v", response)
	}
	encoded, err := json.Marshal(response)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(encoded, []byte(validStart().PrivateKey)) {
		t.Fatal("JIT response contains controller private key")
	}

	backend.jit.Runner.Name = "replacement"
	refused := server.handle(context.Background(), protocolRequest{
		Version:    1,
		Operation:  "generate_jit",
		RunnerName: "smolrunner-job-1",
	})
	if refused.Code != "jit_failed" || refused.EncodedJITConfig != "" {
		t.Fatalf("mismatched JIT response = %#v", refused)
	}

	backend.jit.Runner.Name = "smolrunner-job-1"
	backend.jit.Runner.ID = 0
	refused = server.handle(context.Background(), protocolRequest{
		Version:    1,
		Operation:  "generate_jit",
		RunnerName: "smolrunner-job-1",
	})
	if refused.Code != "jit_failed" || refused.Runner != nil || refused.EncodedJITConfig != "" {
		t.Fatalf("unassigned JIT response = %#v", refused)
	}
}

func TestJITRevalidatesScaleSetPolicyBeforeAndAfterMutation(t *testing.T) {
	backend := &fakeBackend{
		jit: &scaleset.RunnerScaleSetJitRunnerConfig{
			Runner:           &scaleset.RunnerReference{ID: 81, Name: "smolrunner-job-1", RunnerScaleSetID: 23},
			EncodedJITConfig: "one-time-secret",
		},
		validation: []error{nil, errors.New("injected post-JIT drift")},
	}
	server := startedServer(t, backend)
	response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "generate_jit", RunnerName: "smolrunner-job-1", WorkFolder: "_work"})
	if response.Code != "scale_set_drift" || response.EncodedJITConfig != "" || response.Runner != nil || !reflect.DeepEqual(backend.calls, []string{"validate-set", "jit:smolrunner-job-1:_work", "validate-set"}) {
		t.Fatalf("post-JIT drift response=%#v calls=%v", response, backend.calls)
	}
}

func TestObserveRunnerDistinguishesProvenAbsenceFromExactPresence(t *testing.T) {
	backend := &fakeBackend{}
	server := startedServer(t, backend)
	absent := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "observe_runner", RunnerName: "smolrunner-job-1"})
	if absent.Type != "runner_absent" || absent.Runner != nil {
		t.Fatalf("absent response=%#v", absent)
	}

	backend.runner = &scaleset.RunnerReference{ID: 81, Name: "smolrunner-job-1", RunnerScaleSetID: 23}
	present := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "observe_runner", RunnerName: "smolrunner-job-1"})
	if present.Type != "runner" || present.Runner == nil || present.Runner.ID != 81 || present.Runner.Name != "smolrunner-job-1" {
		t.Fatalf("present response=%#v", present)
	}

	backend.runner.Name = "replacement"
	refused := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "observe_runner", RunnerName: "smolrunner-job-1"})
	if refused.Code != "runner_unavailable" || refused.Runner != nil {
		t.Fatalf("rebound response=%#v", refused)
	}
}

func TestRawMessageGateRejectsUnknownLifecycleBeforeOfficialParser(t *testing.T) {
	statistics := `{"totalAvailableJobs":0,"totalAcquiredJobs":0,"totalAssignedJobs":0,"totalRunningJobs":0,"totalRegisteredRunners":0,"totalBusyRunners":0,"totalIdleRunners":0}`
	knownBody, err := json.Marshal([]map[string]any{{
		"messageType":     "JobCompleted",
		"runnerRequestId": 41,
		"repositoryName":  "project",
		"ownerName":       "example",
		"jobId":           "job-1",
		"workflowRunId":   99,
		"requestLabels":   []string{"smolrunner"},
		"result":          "canceled",
	}})
	if err != nil {
		t.Fatal(err)
	}
	knownEnvelope, err := json.Marshal(map[string]any{"messageId": 7, "messageType": "RunnerScaleSetJobMessages", "body": string(knownBody), "statistics": json.RawMessage(statistics)})
	if err != nil {
		t.Fatal(err)
	}
	if err := validateScaleSetMessageBytes(knownEnvelope); err != nil {
		t.Fatalf("known message refused: %v", err)
	}

	unknownBody, err := json.Marshal([]map[string]any{{"messageType": "JobReassigned"}})
	if err != nil {
		t.Fatal(err)
	}
	unknownEnvelope, err := json.Marshal(map[string]any{"messageId": 8, "messageType": "RunnerScaleSetJobMessages", "body": string(unknownBody), "statistics": json.RawMessage(statistics)})
	if err != nil {
		t.Fatal(err)
	}
	if err := validateScaleSetMessageBytes(unknownEnvelope); err == nil {
		t.Fatal("unknown lifecycle type crossed strict raw-message gate")
	}
}

func TestRetryableClientRunsRawGateAndRestoresMessageBody(t *testing.T) {
	statistics := `{"totalAvailableJobs":0,"totalAcquiredJobs":0,"totalAssignedJobs":0,"totalRunningJobs":0,"totalRegisteredRunners":0,"totalBusyRunners":0,"totalIdleRunners":0}`
	unknownBody, err := json.Marshal([]map[string]any{{"messageType": "JobReassigned"}})
	if err != nil {
		t.Fatal(err)
	}
	envelope, err := json.Marshal(map[string]any{"messageId": 8, "messageType": "RunnerScaleSetJobMessages", "body": string(unknownBody), "statistics": json.RawMessage(statistics)})
	if err != nil {
		t.Fatal(err)
	}
	request, err := http.NewRequestWithContext(context.Background(), http.MethodGet, "https://example.invalid/messages", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set(scaleset.HeaderScaleSetMaxCapacity, "1")
	response := &http.Response{StatusCode: http.StatusOK, Request: request, Body: io.NopCloser(bytes.NewReader(envelope))}
	retry, gateErr := newStrictHTTPClient().CheckRetry(context.Background(), response, nil)
	if retry || gateErr == nil {
		t.Fatalf("unknown raw lifecycle was not refused: retry=%t err=%v", retry, gateErr)
	}
	restored, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(restored, envelope) {
		t.Fatal("raw gate did not restore exact response body")
	}
}

func TestScaleSetValidationBindsIdentityLabelsAndUpdatePolicy(t *testing.T) {
	config := validStart()
	set := &scaleset.RunnerScaleSet{
		ID:            config.ScaleSetID,
		Name:          config.ScaleSetName,
		RunnerGroupID: config.RunnerGroupID,
		Labels:        []scaleset.Label{{Name: "smolrunner"}},
		RunnerSetting: scaleset.RunnerSetting{DisableUpdate: true},
	}
	if err := validateScaleSet(set, config); err != nil {
		t.Fatal(err)
	}
	set.RunnerSetting.DisableUpdate = false
	if err := validateScaleSet(set, config); err == nil {
		t.Fatal("accepted scale set with runner auto-update enabled")
	}
}

func TestRunnerRemovalReobservesExactScaleSetIdentity(t *testing.T) {
	backend := &fakeBackend{runner: &scaleset.RunnerReference{ID: 81, Name: "smolrunner-job-1", RunnerScaleSetID: 23}}
	server := startedServer(t, backend)
	response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "remove_runner", RunnerID: 81, RunnerName: "smolrunner-job-1"})
	if response.Type != "removed" || response.Runner == nil || response.Runner.ID != 81 || response.Runner.Name != "smolrunner-job-1" || response.Runner.ScaleSetID != 23 || !reflect.DeepEqual(backend.calls, []string{"runner-id:81", "remove:81"}) {
		t.Fatalf("remove response=%#v calls=%v", response, backend.calls)
	}

	backend.calls = nil
	backend.runner.RunnerScaleSetID = 24
	refused := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "remove_runner", RunnerID: 81, RunnerName: "smolrunner-job-1"})
	if refused.Code != "runner_unavailable" || !reflect.DeepEqual(backend.calls, []string{"runner-id:81"}) {
		t.Fatalf("foreign remove response=%#v calls=%v", refused, backend.calls)
	}

	backend.calls = nil
	backend.runner.RunnerScaleSetID = 23
	refused = server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "remove_runner", RunnerID: 81, RunnerName: "replacement"})
	if refused.Code != "runner_unavailable" || !reflect.DeepEqual(backend.calls, []string{"runner-id:81"}) {
		t.Fatalf("name-rebound remove response=%#v calls=%v", refused, backend.calls)
	}
}

func TestMessageBoundsFailBeforeAckAuthority(t *testing.T) {
	backend := &fakeBackend{message: &scaleset.RunnerScaleSetMessage{
		MessageID:  7,
		Statistics: &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 1},
		JobStartedMessages: []*scaleset.JobStarted{{
			RunnerID:   81,
			RunnerName: "smolrunner-job-1",
			JobMessageBase: scaleset.JobMessageBase{
				RunnerRequestID: 41,
				RepositoryName:  "project",
				OwnerName:       "example",
				JobID:           strings.Repeat("x", 257),
				WorkflowRunID:   99,
				RequestLabels:   []string{"smolrunner"},
			},
		}},
	}}
	server := startedServer(t, backend)
	response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1})
	if response.Code != "invalid_message" || server.pending != nil || !reflect.DeepEqual(backend.calls, []string{"poll"}) {
		t.Fatalf("invalid message response=%#v calls=%v", response, backend.calls)
	}
}

func TestSerializedMessageBoundFailsBeforeAckAuthority(t *testing.T) {
	labels := make([]string, 32)
	for index := range labels {
		labels[index] = strings.Repeat("\\", 100)
	}
	events := make([]*scaleset.JobStarted, 50)
	for index := range events {
		events[index] = &scaleset.JobStarted{
			RunnerID:   index + 1,
			RunnerName: fmt.Sprintf("smolrunner-%d", index+1),
			JobMessageBase: scaleset.JobMessageBase{
				RunnerRequestID: int64(index + 1),
				RepositoryName:  "project",
				OwnerName:       "example",
				JobID:           fmt.Sprintf("job-%d", index+1),
				WorkflowRunID:   int64(index + 1),
				RequestLabels:   labels,
			},
		}
	}
	backend := &fakeBackend{message: &scaleset.RunnerScaleSetMessage{
		MessageID:          7,
		Statistics:         &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 50, TotalRunningJobs: 50},
		JobStartedMessages: events,
	}}
	server := startedServer(t, backend)
	response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1})
	if response.Code != "invalid_message" || server.pending != nil {
		t.Fatalf("oversized serialized message response=%#v pending=%#v", response, server.pending)
	}
}

func TestMessageRequiresExactRunnerLifecycleEvidenceBeforeAck(t *testing.T) {
	backend := &fakeBackend{message: &scaleset.RunnerScaleSetMessage{
		MessageID:  7,
		Statistics: &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 1},
		JobStartedMessages: []*scaleset.JobStarted{{
			JobMessageBase: scaleset.JobMessageBase{
				RunnerRequestID: 41,
				RepositoryName:  "project",
				OwnerName:       "example",
				JobID:           "job-1",
				WorkflowRunID:   99,
				RequestLabels:   []string{"smolrunner"},
			},
		}},
	}}
	server := startedServer(t, backend)
	response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1})
	if response.Code != "invalid_message" || server.pending != nil || !reflect.DeepEqual(backend.calls, []string{"poll"}) {
		t.Fatalf("incomplete start response=%#v calls=%v", response, backend.calls)
	}

	backend.message = &scaleset.RunnerScaleSetMessage{
		MessageID:  8,
		Statistics: &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 1},
		JobCompletedMessages: []*scaleset.JobCompleted{{
			RunnerID:   81,
			RunnerName: "smolrunner-job-1",
			JobMessageBase: scaleset.JobMessageBase{
				RunnerRequestID: 41,
				RepositoryName:  "project",
				OwnerName:       "example",
				JobID:           "job-1",
				WorkflowRunID:   99,
				RequestLabels:   []string{"smolrunner"},
			},
		}},
	}
	response = server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1})
	if response.Code != "invalid_message" || server.pending != nil {
		t.Fatalf("incomplete completion response=%#v", response)
	}
}

func TestMessageAdmitsExactRunnerlessReassignmentCancellation(t *testing.T) {
	backend := &fakeBackend{message: &scaleset.RunnerScaleSetMessage{
		MessageID:  11,
		Statistics: &scaleset.RunnerScaleSetStatistic{},
		JobCompletedMessages: []*scaleset.JobCompleted{{
			Result: "canceled",
			JobMessageBase: scaleset.JobMessageBase{
				RunnerRequestID: 41,
				RepositoryName:  "project",
				OwnerName:       "example",
				JobID:           "job-1",
				WorkflowRunID:   99,
				RequestLabels:   []string{"smolrunner"},
			},
		}},
	}}
	server := startedServer(t, backend)
	response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1})
	if response.Type != "message" || response.MessageID != 11 || len(response.Events) != 1 {
		t.Fatalf("runnerless cancellation response=%#v", response)
	}
	event := response.Events[0]
	if event.Kind != "completed" || event.RunnerID != 0 || event.RunnerName != "" || event.Result != "canceled" || server.pending == nil {
		t.Fatalf("runnerless cancellation event=%#v pending=%#v", event, server.pending)
	}
}

func TestDirectAssignmentWithoutServiceRequestIDGetsStablePrivateJoinIdentity(t *testing.T) {
	base := scaleset.JobMessageBase{
		RunnerRequestID: 0,
		RepositoryName:  "project",
		OwnerName:       "example",
		JobID:           "86290767-4bda-5f2d-a642-b9eebbcc9312",
		WorkflowRunID:   99,
		RequestLabels:   []string{"smolrunner"},
		ScaleSetAssignTime: time.Date(
			2026, time.August, 16, 14, 9, 50, 115613818, time.UTC,
		),
	}
	assigned, err := normalizeJob("assigned", base, 0, "", "")
	if err != nil {
		t.Fatal(err)
	}
	started, err := normalizeJob("started", base, 81, "smolrunner-job-1", "")
	if err != nil {
		t.Fatal(err)
	}
	completed, err := normalizeJob("completed", base, 81, "smolrunner-job-1", "succeeded")
	if err != nil {
		t.Fatal(err)
	}
	if assigned.RunnerRequestID < syntheticRunnerRequestMarker || assigned.RunnerRequestID != started.RunnerRequestID || assigned.RunnerRequestID != completed.RunnerRequestID {
		t.Fatalf("direct assignment identities assigned=%d started=%d completed=%d", assigned.RunnerRequestID, started.RunnerRequestID, completed.RunnerRequestID)
	}
	changed := base
	changed.JobID = "different-job"
	if syntheticRunnerRequestID(changed) == assigned.RunnerRequestID {
		t.Fatal("different exact job evidence reused the synthetic request identity")
	}
	if _, err := normalizeJob("available", base, 0, "", ""); err == nil {
		t.Fatal("available work without the service acquisition identity was accepted")
	}
	changed = base
	changed.ScaleSetAssignTime = changed.ScaleSetAssignTime.Add(time.Second)
	if syntheticRunnerRequestID(changed) == assigned.RunnerRequestID {
		t.Fatal("a later assignment of the same workflow job reused the synthetic request identity")
	}
	reordered := base
	reordered.RequestLabels = []string{"second", "smolrunner"}
	canonical := base
	canonical.RequestLabels = []string{"smolrunner", "second"}
	if syntheticRunnerRequestID(reordered) != syntheticRunnerRequestID(canonical) {
		t.Fatal("equivalent request-label sets changed the synthetic request identity")
	}
}

func TestDirectAssignmentPollAndAckDoNotInvokeAvailableJobAcquisition(t *testing.T) {
	backend := &fakeBackend{message: &scaleset.RunnerScaleSetMessage{
		MessageID:  100000001,
		Statistics: &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 1},
		JobAssignedMessages: []*scaleset.JobAssigned{{JobMessageBase: scaleset.JobMessageBase{
			RunnerRequestID: 0,
			RepositoryName:  "quarry",
			OwnerName:       "Quarry-Labs",
			JobID:           "86290767-4bda-5f2d-a642-b9eebbcc9312",
			WorkflowRunID:   31951901986,
			RequestLabels:   []string{"smolrunner-quarry-pilot"},
			ScaleSetAssignTime: time.Date(
				2026, time.August, 16, 14, 9, 50, 115613818, time.UTC,
			),
		}}},
	}}
	server := startedServer(t, backend)
	response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1})
	if response.Type != "message" || len(response.Events) != 1 || response.Events[0].Kind != "assigned" || response.Events[0].RunnerRequestID < syntheticRunnerRequestMarker || server.pending == nil || len(server.pending.available) != 0 {
		t.Fatalf("direct assignment response=%#v pending=%#v", response, server.pending)
	}
	acked := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "ack", MessageID: response.MessageID})
	if acked.Type != "acked" || len(acked.AcquiredRequests) != 0 || !reflect.DeepEqual(backend.calls, []string{"poll", "delete:100000001"}) {
		t.Fatalf("direct assignment ack=%#v calls=%v", acked, backend.calls)
	}
}

func TestServeNeverWritesPrivateKeyOnStartFailure(t *testing.T) {
	server := newServer(func(context.Context, startConfig) (backend, error) {
		return nil, errors.New("injected start failure containing private key")
	})
	request := protocolRequest{Version: protocolVersion, Operation: "start", Start: validStart()}
	input, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	input = append(input, '\n')
	var output bytes.Buffer
	if err := serve(context.Background(), bytes.NewReader(input), &output, server); err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(output.Bytes(), []byte(validStart().PrivateKey)) {
		t.Fatalf("error response leaked private key: %s", output.Bytes())
	}
	if !bytes.Contains(output.Bytes(), []byte(`"code":"start_failed"`)) {
		t.Fatalf("unexpected response: %s", output.Bytes())
	}
}
