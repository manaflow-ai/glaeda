package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	"github.com/actions/scaleset"
	"github.com/hashicorp/go-retryablehttp"
)

const maxRESTBody = 1024 * 1024
const maxRunnerGroupPages = 10

type runnerGroup struct {
	ID                       int64    `json:"id"`
	Name                     string   `json:"name"`
	Visibility               string   `json:"visibility"`
	Default                  bool     `json:"default"`
	AllowsPublicRepositories bool     `json:"allows_public_repositories"`
	RestrictedToWorkflows    bool     `json:"restricted_to_workflows"`
	SelectedWorkflows        []string `json:"selected_workflows"`
}

type createRunnerGroup struct {
	Name                     string   `json:"name"`
	Visibility               string   `json:"visibility"`
	SelectedRepositoryIDs    []int64  `json:"selected_repository_ids"`
	AllowsPublicRepositories bool     `json:"allows_public_repositories"`
	RestrictedToWorkflows    bool     `json:"restricted_to_workflows"`
	SelectedWorkflows        []string `json:"selected_workflows"`
}

// restAPI is the GitHub REST surface for the org runner group: lookups plus exactly two mutations,
// create and a workflow-list update. There is deliberately no delete.
type restAPI interface {
	RepositoryID(ctx context.Context, org, repo string) (int64, error)
	RunnerGroups(ctx context.Context, org string) ([]runnerGroup, error)
	RunnerGroupRepositoryIDs(ctx context.Context, org string, groupID int64) ([]int64, error)
	CreateRunnerGroup(ctx context.Context, org string, group createRunnerGroup) (runnerGroup, error)
	UpdateRunnerGroupWorkflows(ctx context.Context, org string, groupID int64, workflows []string) (runnerGroup, error)
}

// scaleSetAPI is the Actions service surface, served by the pinned official client. There is
// deliberately no delete.
type scaleSetAPI interface {
	RunnerGroupID(ctx context.Context, name string) (int, error)
	ScaleSet(ctx context.Context, groupID int, name string) (*scaleset.RunnerScaleSet, error)
	CreateScaleSet(ctx context.Context, set *scaleset.RunnerScaleSet) (*scaleset.RunnerScaleSet, error)
}

type restClient struct {
	base  string
	token string
	http  *http.Client
}

func newRESTClient(base, token string) *restClient {
	return &restClient{
		base:  base,
		token: token,
		http: &http.Client{
			Timeout: 30 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
	}
}

func (client *restClient) do(ctx context.Context, method, path string, body any, want int, target any) error {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, client.base+path, reader)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+client.token)
	request.Header.Set("Accept", "application/vnd.github+json")
	request.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	request.Header.Set("User-Agent", "glaeda-scaleset-enroll")
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := client.http.Do(request)
	if err != nil {
		return fmt.Errorf("github_request_failed: %s %s", method, path)
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxRESTBody+1))
	if err != nil || len(raw) > maxRESTBody {
		return fmt.Errorf("github_response_unbounded: %s %s", method, path)
	}
	if response.StatusCode != want {
		// Fixed text only: response bodies are never echoed.
		return fmt.Errorf("github_status_%d: %s %s", response.StatusCode, method, path)
	}
	if target != nil {
		if err := json.Unmarshal(raw, target); err != nil {
			return fmt.Errorf("github_response_invalid: %s %s", method, path)
		}
	}
	return nil
}

func (client *restClient) RepositoryID(ctx context.Context, org, repo string) (int64, error) {
	var result struct {
		ID       int64  `json:"id"`
		FullName string `json:"full_name"`
	}
	if err := client.do(ctx, http.MethodGet, "/repos/"+url.PathEscape(org)+"/"+url.PathEscape(repo), nil, http.StatusOK, &result); err != nil {
		return 0, err
	}
	if result.ID <= 0 || result.FullName != org+"/"+repo {
		return 0, errors.New("repository_identity_mismatch")
	}
	return result.ID, nil
}

func (client *restClient) RunnerGroups(ctx context.Context, org string) ([]runnerGroup, error) {
	var groups []runnerGroup
	for page := 1; page <= maxRunnerGroupPages; page++ {
		var result struct {
			TotalCount   int           `json:"total_count"`
			RunnerGroups []runnerGroup `json:"runner_groups"`
		}
		path := fmt.Sprintf("/orgs/%s/actions/runner-groups?per_page=100&page=%d", url.PathEscape(org), page)
		if err := client.do(ctx, http.MethodGet, path, nil, http.StatusOK, &result); err != nil {
			return nil, err
		}
		groups = append(groups, result.RunnerGroups...)
		if len(result.RunnerGroups) < 100 || len(groups) >= result.TotalCount {
			return groups, nil
		}
	}
	return nil, errors.New("runner_groups_unbounded")
}

func (client *restClient) RunnerGroupRepositoryIDs(ctx context.Context, org string, groupID int64) ([]int64, error) {
	var result struct {
		TotalCount   int `json:"total_count"`
		Repositories []struct {
			ID int64 `json:"id"`
		} `json:"repositories"`
	}
	path := fmt.Sprintf("/orgs/%s/actions/runner-groups/%d/repositories?per_page=100", url.PathEscape(org), groupID)
	if err := client.do(ctx, http.MethodGet, path, nil, http.StatusOK, &result); err != nil {
		return nil, err
	}
	if result.TotalCount != len(result.Repositories) {
		return nil, errors.New("runner_group_repositories_unbounded")
	}
	ids := make([]int64, 0, len(result.Repositories))
	for _, repository := range result.Repositories {
		ids = append(ids, repository.ID)
	}
	return ids, nil
}

func (client *restClient) CreateRunnerGroup(ctx context.Context, org string, group createRunnerGroup) (runnerGroup, error) {
	var created runnerGroup
	err := client.do(ctx, http.MethodPost, "/orgs/"+url.PathEscape(org)+"/actions/runner-groups", group, http.StatusCreated, &created)
	return created, err
}

func (client *restClient) UpdateRunnerGroupWorkflows(ctx context.Context, org string, groupID int64, workflows []string) (runnerGroup, error) {
	var updated runnerGroup
	body := map[string]any{"restricted_to_workflows": true, "selected_workflows": workflows}
	err := client.do(ctx, http.MethodPatch, fmt.Sprintf("/orgs/%s/actions/runner-groups/%d", url.PathEscape(org), groupID), body, http.StatusOK, &updated)
	return updated, err
}

type officialScaleSetAPI struct {
	client *scaleset.Client
}

func newOfficialScaleSetAPI(org, token string) (*officialScaleSetAPI, error) {
	client, err := scaleset.NewClientWithPersonalAccessToken(scaleset.NewClientWithPersonalAccessTokenConfig{
		GitHubConfigURL:     "https://github.com/" + org,
		PersonalAccessToken: token,
		SystemInfo: scaleset.SystemInfo{
			System:    "smolrunner",
			Version:   "0.1.0",
			Subsystem: "enroll",
		},
	}, scaleset.WithRetryableHTTPClint(noRetryHTTPClient()))
	if err != nil {
		return nil, err
	}
	return &officialScaleSetAPI{client: client}, nil
}

func (api *officialScaleSetAPI) RunnerGroupID(ctx context.Context, name string) (int, error) {
	group, err := api.client.GetRunnerGroupByName(ctx, name)
	if err != nil {
		return 0, errors.New("actions_runner_group_unavailable")
	}
	if group == nil || group.ID <= 0 || group.Name != name {
		return 0, errors.New("actions_runner_group_mismatch")
	}
	return group.ID, nil
}

func (api *officialScaleSetAPI) ScaleSet(ctx context.Context, groupID int, name string) (*scaleset.RunnerScaleSet, error) {
	set, err := api.client.GetRunnerScaleSet(ctx, groupID, name)
	if err != nil {
		return nil, errors.New("scale_set_lookup_failed")
	}
	return set, nil
}

func (api *officialScaleSetAPI) CreateScaleSet(ctx context.Context, set *scaleset.RunnerScaleSet) (*scaleset.RunnerScaleSet, error) {
	created, err := api.client.CreateRunnerScaleSet(ctx, set)
	if err != nil {
		return nil, errors.New("scale_set_create_failed")
	}
	return created, nil
}

// noRetryHTTPClient never replays a request: a replayed create after a lost response could otherwise
// race the idempotent re-observation that a rerun performs.
func noRetryHTTPClient() *retryablehttp.Client {
	client := retryablehttp.NewClient()
	client.RetryMax = 0
	client.Logger = nil
	client.HTTPClient.CheckRedirect = func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}
	return client
}
