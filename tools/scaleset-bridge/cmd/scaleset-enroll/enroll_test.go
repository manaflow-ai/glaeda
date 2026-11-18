package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/actions/scaleset"
)

const repoID = 777
const mainPin = "manaflow-ai/cmux/.github/workflows/ci.yml@refs/heads/main"
const nextPin = "manaflow-ai/cmux/.github/workflows/iroh-v2-production-drift.yml@refs/heads/feat-cmux-next"

// fakeGitHub is an in-memory GitHub REST server for the runner group surface.
type fakeGitHub struct {
	mu        sync.Mutex
	groups    []runnerGroup
	groupRepo map[int64][]int64
	calls     []string
	auth      []string
	nextID    int64
}

func (fake *fakeGitHub) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	fake.calls = append(fake.calls, request.Method+" "+request.URL.Path)
	fake.auth = append(fake.auth, request.Header.Get("Authorization"))
	write := func(status int, value any) {
		writer.WriteHeader(status)
		_ = json.NewEncoder(writer).Encode(value)
	}
	switch {
	case request.Method == http.MethodGet && request.URL.Path == "/repos/manaflow-ai/cmux":
		write(http.StatusOK, map[string]any{"id": repoID, "full_name": "manaflow-ai/cmux"})
	case request.Method == http.MethodGet && request.URL.Path == "/orgs/manaflow-ai/actions/runner-groups":
		write(http.StatusOK, map[string]any{"total_count": len(fake.groups), "runner_groups": fake.groups})
	case request.Method == http.MethodPost && request.URL.Path == "/orgs/manaflow-ai/actions/runner-groups":
		var body createRunnerGroup
		if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
			write(http.StatusUnprocessableEntity, nil)
			return
		}
		fake.nextID++
		group := runnerGroup{ID: fake.nextID, Name: body.Name, Visibility: body.Visibility, AllowsPublicRepositories: body.AllowsPublicRepositories, RestrictedToWorkflows: body.RestrictedToWorkflows, SelectedWorkflows: body.SelectedWorkflows}
		fake.groups = append(fake.groups, group)
		fake.groupRepo[group.ID] = body.SelectedRepositoryIDs
		write(http.StatusCreated, group)
	case strings.HasSuffix(request.URL.Path, "/repositories"):
		var id int64
		fmt.Sscanf(strings.TrimPrefix(request.URL.Path, "/orgs/manaflow-ai/actions/runner-groups/"), "%d", &id)
		repos := []map[string]any{}
		for _, repo := range fake.groupRepo[id] {
			repos = append(repos, map[string]any{"id": repo})
		}
		write(http.StatusOK, map[string]any{"total_count": len(repos), "repositories": repos})
	case request.Method == http.MethodPatch:
		var id int64
		fmt.Sscanf(strings.TrimPrefix(request.URL.Path, "/orgs/manaflow-ai/actions/runner-groups/"), "%d", &id)
		var body struct {
			Restricted bool     `json:"restricted_to_workflows"`
			Workflows  []string `json:"selected_workflows"`
		}
		_ = json.NewDecoder(request.Body).Decode(&body)
		for index := range fake.groups {
			if fake.groups[index].ID == id {
				fake.groups[index].RestrictedToWorkflows = body.Restricted
				fake.groups[index].SelectedWorkflows = body.Workflows
				write(http.StatusOK, fake.groups[index])
				return
			}
		}
		write(http.StatusNotFound, nil)
	default:
		write(http.StatusNotFound, nil)
	}
}

func (fake *fakeGitHub) mutations() []string {
	var out []string
	for _, call := range fake.calls {
		if !strings.HasPrefix(call, "GET ") {
			out = append(out, call)
		}
	}
	return out
}

type fakeActions struct {
	groups  map[string]int
	sets    []scaleset.RunnerScaleSet
	created int
}

func (fake *fakeActions) RunnerGroupID(_ context.Context, name string) (int, error) {
	id, ok := fake.groups[name]
	if !ok {
		return 0, fmt.Errorf("missing")
	}
	return id, nil
}

func (fake *fakeActions) ScaleSet(_ context.Context, groupID int, name string) (*scaleset.RunnerScaleSet, error) {
	for index := range fake.sets {
		if fake.sets[index].RunnerGroupID == groupID && fake.sets[index].Name == name {
			copy := fake.sets[index]
			return &copy, nil
		}
	}
	return nil, nil
}

func (fake *fakeActions) CreateScaleSet(_ context.Context, set *scaleset.RunnerScaleSet) (*scaleset.RunnerScaleSet, error) {
	fake.created++
	created := *set
	created.ID = 40 + fake.created
	fake.sets = append(fake.sets, created)
	return &created, nil
}

func baseOptions() options {
	return options{
		Org:         "manaflow-ai",
		Repository:  "cmux",
		RunnerGroup: "cmux-fleet-linux",
		ScaleSet:    "glaeda-linux-arm64",
		Labels:      []string{"glaeda-linux-arm64"},
		Workflows:   []string{nextPin, mainPin},
	}
}

func harness(t *testing.T) (*fakeGitHub, *restClient, *fakeActions) {
	t.Helper()
	fake := &fakeGitHub{groupRepo: map[int64][]int64{}, nextID: 10}
	server := httptest.NewServer(fake)
	t.Cleanup(server.Close)
	actions := &fakeActions{groups: map[string]int{}}
	return fake, newRESTClient(server.URL, "ghs_testtoken"), actions
}

func TestPlanCreatesNothingAndReportsBothCreations(t *testing.T) {
	github, rest, actions := harness(t)
	result, err := run(context.Background(), baseOptions(), rest, actions)
	if err != nil {
		t.Fatal(err)
	}
	if result.Mode != "plan" || result.RunnerGroup.Action != "create" || result.ScaleSet.Action != "create" || result.Enrollment != nil {
		t.Fatalf("plan result = %#v", result)
	}
	if mutations := github.mutations(); len(mutations) != 0 || actions.created != 0 {
		t.Fatalf("plan mutated: %v created=%d", mutations, actions.created)
	}
	for _, header := range github.auth {
		if header != "Bearer ghs_testtoken" {
			t.Fatalf("authorization header = %q", header)
		}
	}
}

func TestApplyCreatesTrustedGroupAndScaleSetThenIsIdempotent(t *testing.T) {
	github, rest, actions := harness(t)
	opts := baseOptions()
	opts.Apply = true
	// The Actions service sees the group the REST create made.
	actions.groups["cmux-fleet-linux"] = 11
	result, err := run(context.Background(), opts, rest, actions)
	if err != nil {
		t.Fatal(err)
	}
	created := github.groups[0]
	if created.Visibility != "selected" || created.AllowsPublicRepositories || !created.RestrictedToWorkflows ||
		!reflect.DeepEqual(created.SelectedWorkflows, []string{mainPin, nextPin}) || !reflect.DeepEqual(github.groupRepo[11], []int64{repoID}) {
		t.Fatalf("created group = %#v repos=%v", created, github.groupRepo[11])
	}
	set := actions.sets[0]
	if !set.RunnerSetting.DisableUpdate || set.RunnerGroupID != 11 || len(set.Labels) != 1 || set.Labels[0].Name != "glaeda-linux-arm64" {
		t.Fatalf("created scale set = %#v", set)
	}
	want := &enrollmentResult{ScaleSetID: 41, ScaleSetName: "glaeda-linux-arm64", RunnerGroupID: 11, Labels: []string{"glaeda-linux-arm64"}, Owner: "manaflow-ai", Repository: "cmux"}
	if !reflect.DeepEqual(result.Enrollment, want) {
		t.Fatalf("enrollment = %#v", result.Enrollment)
	}

	github.calls = nil
	again, err := run(context.Background(), opts, rest, actions)
	if err != nil || again.RunnerGroup.Action != "exists" || again.ScaleSet.Action != "exists" || !reflect.DeepEqual(again.Enrollment, want) {
		t.Fatalf("second apply = %#v err=%v", again, err)
	}
	if mutations := github.mutations(); len(mutations) != 0 || actions.created != 1 {
		t.Fatalf("idempotent apply mutated: %v created=%d", mutations, actions.created)
	}
}

func TestExistingForeignGroupShapeIsRefusedWithoutMutation(t *testing.T) {
	cases := map[string]func(*fakeGitHub){
		"all repositories": func(fake *fakeGitHub) { fake.groups[0].Visibility = "all" },
		"public repos":     func(fake *fakeGitHub) { fake.groups[0].AllowsPublicRepositories = true },
		"default group":    func(fake *fakeGitHub) { fake.groups[0].Default = true },
		"another repo":     func(fake *fakeGitHub) { fake.groupRepo[5] = []int64{repoID, 778} },
	}
	for name, mutate := range cases {
		github, rest, actions := harness(t)
		github.groups = []runnerGroup{{ID: 5, Name: "cmux-fleet-linux", Visibility: "selected", RestrictedToWorkflows: true, SelectedWorkflows: []string{mainPin, nextPin}}}
		github.groupRepo[5] = []int64{repoID}
		mutate(github)
		opts := baseOptions()
		opts.Apply = true
		opts.UpdateWorkflows = true
		_, err := run(context.Background(), opts, rest, actions)
		if code, next := errorCode(err); code != "runner_group_conflict" || !strings.Contains(next, "glaeda-linux-trusted") {
			t.Fatalf("%s: code=%s next=%s", name, code, next)
		}
		if len(github.mutations()) != 0 || actions.created != 0 {
			t.Fatalf("%s: mutated", name)
		}
	}
}

func TestWorkflowListChangeNeedsExplicitUpdate(t *testing.T) {
	github, rest, actions := harness(t)
	github.groups = []runnerGroup{{ID: 5, Name: "cmux-fleet-linux", Visibility: "selected", RestrictedToWorkflows: true, SelectedWorkflows: []string{mainPin}}}
	github.groupRepo[5] = []int64{repoID}
	actions.groups["cmux-fleet-linux"] = 5
	opts := baseOptions()
	opts.Apply = true
	if _, err := run(context.Background(), opts, rest, actions); err == nil {
		t.Fatal("changed workflow list without --update-workflows")
	} else if code, _ := errorCode(err); code != "runner_group_workflows_differ" {
		t.Fatalf("code = %s", code)
	}
	if len(github.mutations()) != 0 {
		t.Fatalf("mutated: %v", github.mutations())
	}
	opts.UpdateWorkflows = true
	result, err := run(context.Background(), opts, rest, actions)
	if err != nil || result.RunnerGroup.Action != "update_workflows" || !reflect.DeepEqual(result.RunnerGroup.PreviousWorkflows, []string{mainPin}) {
		t.Fatalf("update result = %#v err=%v", result, err)
	}
	if !reflect.DeepEqual(github.groups[0].SelectedWorkflows, []string{mainPin, nextPin}) {
		t.Fatalf("workflows = %v", github.groups[0].SelectedWorkflows)
	}
}

func TestExistingScaleSetWithAnotherShapeIsRefused(t *testing.T) {
	github, rest, actions := harness(t)
	github.groups = []runnerGroup{{ID: 5, Name: "cmux-fleet-linux", Visibility: "selected", RestrictedToWorkflows: true, SelectedWorkflows: []string{mainPin, nextPin}}}
	github.groupRepo[5] = []int64{repoID}
	actions.groups["cmux-fleet-linux"] = 5
	actions.sets = []scaleset.RunnerScaleSet{{ID: 9, Name: "glaeda-linux-arm64", RunnerGroupID: 5, Labels: []scaleset.Label{{Name: "glaeda-linux-arm64"}}}}
	opts := baseOptions()
	opts.Apply = true
	if _, err := run(context.Background(), opts, rest, actions); err == nil {
		t.Fatal("accepted a scale set that allows runner self-update")
	} else if code, _ := errorCode(err); code != "scale_set_conflict" {
		t.Fatalf("code = %s", code)
	}
	if actions.created != 0 {
		t.Fatal("created a second scale set")
	}
}

func TestUntrustedWorkflowPinsFailBeforeAnyGitHubCall(t *testing.T) {
	for _, pin := range []string{
		"manaflow-ai/cmux/.github/workflows/ci.yml@refs/heads/feature",
		"manaflow-ai/cmux/.github/workflows/ci.yml@refs/pull/1/merge",
		"manaflow-ai/cmux/.github/workflows/ci.yml@refs/tags/v1",
		"manaflow-ai/cmux/.github/workflows/ci.yml@0123456789abcdef0123456789abcdef01234567",
		"manaflow-ai/cmux/.github/workflows/ci.yml",
		"manaflow-ai/other/.github/workflows/ci.yml@refs/heads/main",
		"manaflow-ai/cmux/.github/workflows/../ci.yml@refs/heads/main",
		"manaflow-ai/cmux/.github/workflows/ci.yml@refs/heads/main@refs/heads/main",
	} {
		github, rest, actions := harness(t)
		opts := baseOptions()
		opts.Workflows = []string{mainPin, pin}
		_, err := run(context.Background(), opts, rest, actions)
		if code, _ := errorCode(err); code != "untrusted_workflow" {
			t.Fatalf("%s: code = %s", pin, code)
		}
		if len(github.calls) != 0 {
			t.Fatalf("%s: GitHub was called", pin)
		}
	}
	github, rest, actions := harness(t)
	opts := baseOptions()
	opts.Workflows = nil
	if _, err := run(context.Background(), opts, rest, actions); err == nil || len(github.calls) != 0 {
		t.Fatal("empty workflow list accepted")
	}
}

func writeTokenFile(t *testing.T, mode os.FileMode, document map[string]any) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "token.json")
	raw, _ := json.Marshal(document)
	if err := os.WriteFile(path, raw, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
	return path
}

func tokenDocument(now time.Time) map[string]any {
	return map[string]any{
		"app_id":      defaultAppID,
		"org":         "manaflow-ai",
		"token":       "ghs_pushedtoken",
		"expires_at":  now.Add(50 * time.Minute).UTC().Format(time.RFC3339),
		"permissions": map[string]string{"organization_self_hosted_runners": "write", "metadata": "read"},
		"minted_at":   now.Add(-10 * time.Minute).UTC().Format(time.RFC3339),
	}
}

func TestTokenFileReadsPushedShape(t *testing.T) {
	now := time.Now()
	token, err := readTokenFile(writeTokenFile(t, 0o600, tokenDocument(now)), defaultAppID, now)
	if err != nil || token.value != "ghs_pushedtoken" {
		t.Fatalf("token=%#v err=%v", token, err)
	}
}

func TestTokenFileRefusals(t *testing.T) {
	now := time.Now()
	cases := map[string]func(map[string]any) (os.FileMode, string){
		"group readable": func(map[string]any) (os.FileMode, string) { return 0o640, "token_file_unsafe" },
		"wrong app": func(document map[string]any) (os.FileMode, string) {
			document["app_id"] = 1
			return 0o600, "token_file_wrong_app"
		},
		"expiring": func(document map[string]any) (os.FileMode, string) {
			document["expires_at"] = now.Add(time.Minute).UTC().Format(time.RFC3339)
			return 0o600, "token_file_expiring"
		},
		"read only permission": func(document map[string]any) (os.FileMode, string) {
			document["permissions"] = map[string]string{"organization_self_hosted_runners": "read"}
			return 0o600, "token_file_lacks_runner_write"
		},
		"unknown field": func(document map[string]any) (os.FileMode, string) {
			document["private_key"] = "never"
			return 0o600, "token_file_invalid"
		},
		"malformed org": func(document map[string]any) (os.FileMode, string) {
			document["org"] = "-not a login"
			return 0o600, "token_file_invalid"
		},
		"multi token shape": func(document map[string]any) (os.FileMode, string) {
			delete(document, "token")
			document["tokens"] = []any{}
			return 0o600, "token_file_invalid"
		},
	}
	for name, mutate := range cases {
		document := tokenDocument(now)
		mode, want := mutate(document)
		_, err := readTokenFile(writeTokenFile(t, mode, document), defaultAppID, now)
		if err == nil || err.Error() != want {
			t.Fatalf("%s: err=%v want %s", name, err, want)
		}
	}
	target := writeTokenFile(t, 0o600, tokenDocument(now))
	link := filepath.Join(t.TempDir(), "link.json")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	if _, err := readTokenFile(link, defaultAppID, now); err == nil {
		t.Fatal("followed a symlink")
	}
	if _, err := readTokenFile("relative.json", defaultAppID, now); err == nil {
		t.Fatal("accepted a relative path")
	}
}

func TestCommandNeverPrintsTheToken(t *testing.T) {
	now := time.Now()
	document := tokenDocument(now)
	document["app_id"] = 1
	path := writeTokenFile(t, 0o600, document)
	var stdout, stderr bytes.Buffer
	code := mainWithArgs([]string{"--token-file", path, "--workflow", mainPin}, &stdout, &stderr)
	if code != 1 || !strings.Contains(stdout.String(), `"token_file_wrong_app"`) {
		t.Fatalf("code=%d out=%s", code, stdout.String())
	}
	all, _ := io.ReadAll(io.MultiReader(&stdout, &stderr))
	if bytes.Contains(all, []byte("ghs_pushedtoken")) {
		t.Fatal("output carried the token")
	}
	if mainWithArgs([]string{"extra"}, io.Discard, io.Discard) != 2 {
		t.Fatal("accepted a positional argument")
	}
}
