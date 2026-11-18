package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"testing"
	"time"

	"github.com/actions/scaleset"
)

const testInstallationToken = "ghs_exampleInstallationToken0123456789"

var testNow = time.Unix(1_800_000_000, 0)

func validTokenStart(life time.Duration) startConfig {
	config := validStart()
	config.ClientID = ""
	config.InstallationID = 0
	config.PrivateKey = ""
	config.InstallationToken = testInstallationToken
	config.InstallationTokenExpiresAt = testNow.Add(life).Unix()
	return config
}

func tokenServer(fake *fakeBackend, clock *time.Time, seen *startConfig) *server {
	if fake.initial == nil {
		fake.initial = &scaleset.RunnerScaleSetStatistic{}
	}
	server := newServer(func(_ context.Context, config startConfig) (backend, error) {
		if seen != nil {
			*seen = config
		}
		return fake, nil
	})
	server.now = func() time.Time { return *clock }
	return server
}

func TestStartConfigRequiresExactlyOneCredentialKind(t *testing.T) {
	if err := validTokenStart(time.Hour).validate(); err != nil {
		t.Fatalf("token start rejected: %v", err)
	}
	if err := validStart().validate(); err != nil {
		t.Fatalf("App key start rejected: %v", err)
	}
	cases := map[string]func(*startConfig){
		"token with key":          func(config *startConfig) { config.PrivateKey = "key" },
		"token with client id":    func(config *startConfig) { config.ClientID = "Iv1.example" },
		"token with installation": func(config *startConfig) { config.InstallationID = 17 },
		"token without expiry":    func(config *startConfig) { config.InstallationTokenExpiresAt = 0 },
		"expiry without token":    func(config *startConfig) { config.InstallationToken = "" },
		"token with whitespace":   func(config *startConfig) { config.InstallationToken = "ghs_a b" },
		"token with newline":      func(config *startConfig) { config.InstallationToken = "ghs_a\n" },
		"token over bound": func(config *startConfig) {
			config.InstallationToken = string(bytes.Repeat([]byte("a"), maxInstallationTokenBytes+1))
		},
		"token with negative expiry": func(config *startConfig) { config.InstallationTokenExpiresAt = -1 },
	}
	for name, mutate := range cases {
		config := validTokenStart(time.Hour)
		mutate(&config)
		if err := config.validate(); err == nil {
			t.Fatalf("%s: accepted", name)
		}
	}
	keyWithExpiry := validStart()
	keyWithExpiry.InstallationTokenExpiresAt = testNow.Add(time.Hour).Unix()
	if err := keyWithExpiry.validate(); err == nil {
		t.Fatal("App key start with a token expiry accepted")
	}
}

func TestTokenStartReachesBackendAndClearsRetainedSecret(t *testing.T) {
	clock := testNow
	var seen startConfig
	fake := &fakeBackend{}
	server := tokenServer(fake, &clock, &seen)
	response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "start", Start: validTokenStart(50 * time.Minute)})
	if response.Type != "ready" {
		t.Fatalf("start response = %#v", response)
	}
	if seen.InstallationToken != testInstallationToken || seen.PrivateKey != "" {
		t.Fatalf("backend did not receive exactly the token credential")
	}
	if server.config.InstallationToken != "" || server.config.PrivateKey != "" {
		t.Fatal("server retained a credential after start")
	}
	if !server.credentialExpiresAt.Equal(testNow.Add(50 * time.Minute)) {
		t.Fatalf("expiry = %v", server.credentialExpiresAt)
	}
}

func TestTokenStartRefusesShortLivedTokenBeforeAnyServiceCall(t *testing.T) {
	clock := testNow
	called := false
	server := newServer(func(context.Context, startConfig) (backend, error) {
		called = true
		return nil, errors.New("unexpected")
	})
	server.now = func() time.Time { return clock }
	for _, life := range []time.Duration{-time.Minute, 0, minInstallationTokenStartLife - time.Second} {
		response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "start", Start: validTokenStart(life)})
		if response.Type != "error" || response.Code != "credential_expiring" {
			t.Fatalf("life %v: response = %#v", life, response)
		}
	}
	if called {
		t.Fatal("short-lived token reached the service")
	}
}

func TestPollEndsTokenSessionNearExpiryWithoutServiceCall(t *testing.T) {
	clock := testNow
	fake := &fakeBackend{}
	server := tokenServer(fake, &clock, nil)
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "start", Start: validTokenStart(30 * time.Minute)}); response.Type != "ready" {
		t.Fatalf("start response = %#v", response)
	}
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1}); response.Type != "idle" {
		t.Fatalf("fresh poll response = %#v", response)
	}
	clock = testNow.Add(30*time.Minute - installationTokenPollMargin + time.Second)
	response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1})
	if response.Type != "error" || response.Code != "credential_expiring" {
		t.Fatalf("near-expiry poll response = %#v", response)
	}
	if !reflect.DeepEqual(fake.calls, []string{"poll"}) {
		t.Fatalf("near-expiry poll reached the service: %v", fake.calls)
	}
}

func TestTokenExpiryNeverInterruptsAnOutstandingAck(t *testing.T) {
	clock := testNow
	fake := &fakeBackend{
		acquired: []int64{41},
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
	server := tokenServer(fake, &clock, nil)
	server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "start", Start: validTokenStart(20 * time.Minute)})
	if message := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1}); message.Type != "message" {
		t.Fatalf("message response = %#v", message)
	}
	clock = testNow.Add(15 * time.Minute)
	if acked := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "ack", MessageID: 7}); acked.Type != "acked" {
		t.Fatalf("ack after the poll margin = %#v", acked)
	}
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1}); response.Code != "credential_expiring" {
		t.Fatalf("next poll response = %#v", response)
	}
}

func TestAppKeySessionHasNoTokenExpiry(t *testing.T) {
	clock := testNow
	fake := &fakeBackend{}
	server := tokenServer(fake, &clock, nil)
	server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "start", Start: validStart()})
	clock = testNow.Add(24 * time.Hour)
	if response := server.handle(context.Background(), protocolRequest{Version: protocolVersion, Operation: "poll", MaxCapacity: 1}); response.Type != "idle" {
		t.Fatalf("App key poll response = %#v", response)
	}
}

func TestServeNeverWritesInstallationTokenOnStartFailure(t *testing.T) {
	server := newServer(func(context.Context, startConfig) (backend, error) {
		return nil, errors.New("injected start failure")
	})
	start := validTokenStart(time.Hour)
	start.InstallationTokenExpiresAt = time.Now().Add(time.Hour).Unix()
	input, err := json.Marshal(protocolRequest{Version: protocolVersion, Operation: "start", Start: start})
	if err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	if err := serve(context.Background(), bytes.NewReader(append(input, '\n')), &output, server); err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(output.Bytes(), []byte(testInstallationToken)) {
		t.Fatalf("response leaked the installation token: %s", output.Bytes())
	}
	if !bytes.Contains(output.Bytes(), []byte(`"code":"start_failed"`)) {
		t.Fatalf("unexpected response: %s", output.Bytes())
	}
}

func TestOfficialClientAcceptsInstallationTokenCredential(t *testing.T) {
	client, err := newOfficialClient(validTokenStart(time.Hour), newStrictHTTPClient())
	if err != nil || client == nil {
		t.Fatalf("token client: %v", err)
	}
}
