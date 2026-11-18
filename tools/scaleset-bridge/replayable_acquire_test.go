package main

import (
	"context"
	"reflect"
	"testing"

	"github.com/actions/scaleset"
)

func TestDecodeReplayableAcquireRequest(t *testing.T) {
	request, err := decodeRequest([]byte(`{"version":3,"operation":"acquire","runner_request_ids":[41,42]}`))
	if err != nil {
		t.Fatalf("decode acquire request: %v", err)
	}
	if request.Operation != "acquire" || !reflect.DeepEqual(request.RunnerRequestIDs, []int64{41, 42}) {
		t.Fatalf("decoded request = %#v", request)
	}
}

func TestReplayableAcquireUsesDurableRequestIDsWithoutPendingMessage(t *testing.T) {
	backend := &fakeBackend{acquired: []int64{42}}
	server := startedServer(t, backend)

	response := server.handle(context.Background(), protocolRequest{
		Version:          protocolVersion,
		Operation:        "acquire",
		RunnerRequestIDs: []int64{41, 42},
	})
	if response.Type != "acquired" || !reflect.DeepEqual(response.AcquiredRequests, []int64{42}) {
		t.Fatalf("acquire response = %#v", response)
	}
	if !reflect.DeepEqual(backend.calls, []string{"acquire:[41 42]"}) {
		t.Fatalf("acquire calls = %v", backend.calls)
	}
}

func TestReplayableAcquireAllowsAlreadyAcquiredEmptySubset(t *testing.T) {
	backend := &fakeBackend{acquired: []int64{}}
	server := startedServer(t, backend)

	response := server.handle(context.Background(), protocolRequest{
		Version:          protocolVersion,
		Operation:        "acquire",
		RunnerRequestIDs: []int64{41},
	})
	if response.Type != "acquired" || len(response.AcquiredRequests) != 0 {
		t.Fatalf("replayed acquire response = %#v", response)
	}
}

func TestReplayableAcquireRefusesPendingMessage(t *testing.T) {
	backend := &fakeBackend{
		message: &scaleset.RunnerScaleSetMessage{
			MessageID:  7,
			Statistics: &scaleset.RunnerScaleSetStatistic{TotalAvailableJobs: 1},
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
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1}); response.Type != "message" {
		t.Fatalf("poll response = %#v", response)
	}
	response := server.handle(context.Background(), protocolRequest{
		Version:          protocolVersion,
		Operation:        "acquire",
		RunnerRequestIDs: []int64{41},
	})
	if response.Code != "ack_required" {
		t.Fatalf("pending acquire response = %#v", response)
	}
	if !reflect.DeepEqual(backend.calls, []string{"poll"}) {
		t.Fatalf("pending acquire reached backend: %v", backend.calls)
	}
}

func TestReplayableAcquireRejectsInvalidRequestIDsBeforeMutation(t *testing.T) {
	for _, requestIDs := range [][]int64{{}, {0}, {41, 41}} {
		backend := &fakeBackend{}
		server := startedServer(t, backend)
		response := server.handle(context.Background(), protocolRequest{
			Version:          protocolVersion,
			Operation:        "acquire",
			RunnerRequestIDs: requestIDs,
		})
		if response.Code != "invalid_acquisition_request" {
			t.Fatalf("request %v response = %#v", requestIDs, response)
		}
		if len(backend.calls) != 0 {
			t.Fatalf("request %v reached backend: %v", requestIDs, backend.calls)
		}
	}
}

func TestReplayableAcquireRejectsForeignServiceResult(t *testing.T) {
	backend := &fakeBackend{acquired: []int64{43}}
	server := startedServer(t, backend)
	response := server.handle(context.Background(), protocolRequest{
		Version:          protocolVersion,
		Operation:        "acquire",
		RunnerRequestIDs: []int64{41, 42},
	})
	if response.Code != "invalid_acquisition" {
		t.Fatalf("foreign acquisition response = %#v", response)
	}
}
