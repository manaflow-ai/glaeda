package main

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"slices"

	"github.com/actions/scaleset"
)

const outputSchema = "glaeda.scaleset-enroll.v1"

// trustedRefs are the protected bases whose workflows may reach the scale set. Pull request refs,
// tags, SHAs and every other branch are refused: until the guest egress gate (M4) exists, only
// trusted base-branch workflow code may run on these VMs.
var trustedRefs = []string{"refs/heads/main", "refs/heads/feat-cmux-next"}

var namePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`)
var workflowFilePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml$`)

type options struct {
	Org             string
	Repository      string
	RunnerGroup     string
	ScaleSet        string
	Labels          []string
	Workflows       []string
	Apply           bool
	UpdateWorkflows bool
}

type groupResult struct {
	Name              string   `json:"name"`
	Action            string   `json:"action"`
	ID                *int64   `json:"id"`
	ActionsID         *int     `json:"actions_id"`
	SelectedWorkflows []string `json:"selected_workflows"`
	PreviousWorkflows []string `json:"previous_workflows,omitempty"`
}

type scaleSetResult struct {
	Name   string   `json:"name"`
	Action string   `json:"action"`
	ID     *int     `json:"id"`
	Labels []string `json:"labels"`
}

type enrollmentResult struct {
	ScaleSetID    int      `json:"scale_set_id"`
	ScaleSetName  string   `json:"scale_set_name"`
	RunnerGroupID int      `json:"runner_group_id"`
	Labels        []string `json:"labels"`
	Owner         string   `json:"owner"`
	Repository    string   `json:"repository"`
}

type output struct {
	Schema      string            `json:"schema"`
	Mode        string            `json:"mode"`
	Org         string            `json:"org"`
	Repository  string            `json:"repository"`
	RunnerGroup *groupResult      `json:"runner_group,omitempty"`
	ScaleSet    *scaleSetResult   `json:"scale_set,omitempty"`
	Enrollment  *enrollmentResult `json:"enrollment"`
	Error       string            `json:"error,omitempty"`
	Next        string            `json:"next,omitempty"`
}

type enrollError struct {
	code string
	next string
}

func (err enrollError) Error() string { return err.code }

// validate rejects every invalid input before the first GitHub call.
func (opts *options) validate() error {
	if !namePattern.MatchString(opts.Org) || !namePattern.MatchString(opts.Repository) ||
		!namePattern.MatchString(opts.RunnerGroup) || !namePattern.MatchString(opts.ScaleSet) {
		return enrollError{code: "invalid_identity"}
	}
	if len(opts.Labels) == 0 || len(opts.Labels) > 8 {
		return enrollError{code: "invalid_labels"}
	}
	for _, label := range opts.Labels {
		if !namePattern.MatchString(label) {
			return enrollError{code: "invalid_labels"}
		}
	}
	opts.Labels = sortedUnique(opts.Labels)
	if len(opts.Workflows) == 0 || len(opts.Workflows) > 32 {
		return enrollError{code: "invalid_workflows", next: "pass one or more --workflow ORG/REPO/.github/workflows/FILE.yml@refs/heads/main"}
	}
	prefix := opts.Org + "/" + opts.Repository + "/.github/workflows/"
	for _, workflow := range opts.Workflows {
		if !trustedWorkflowPin(workflow, prefix) {
			return enrollError{code: "untrusted_workflow", next: "pin each workflow to " + prefix + "FILE.yml@refs/heads/main or @refs/heads/feat-cmux-next"}
		}
	}
	opts.Workflows = sortedUnique(opts.Workflows)
	return nil
}

func trustedWorkflowPin(workflow, prefix string) bool {
	if len(workflow) <= len(prefix) || workflow[:len(prefix)] != prefix {
		return false
	}
	rest := workflow[len(prefix):]
	for _, ref := range trustedRefs {
		suffix := "@" + ref
		if len(rest) > len(suffix) && rest[len(rest)-len(suffix):] == suffix {
			return workflowFilePattern.MatchString(rest[:len(rest)-len(suffix)])
		}
	}
	return false
}

func sortedUnique(values []string) []string {
	out := append([]string(nil), values...)
	slices.Sort(out)
	return slices.Compact(out)
}

// run observes, plans and (with --apply) converges the runner group and scale set. It only ever
// creates a group or scale set, or replaces the workflow list of a group the operator named with
// --update-workflows. It never deletes, never widens repository visibility, and refuses any existing
// object whose shape differs from the trusted shape.
func run(ctx context.Context, opts options, rest restAPI, actions scaleSetAPI) (output, error) {
	result := output{Schema: outputSchema, Mode: "plan", Org: opts.Org, Repository: opts.Org + "/" + opts.Repository}
	if opts.Apply {
		result.Mode = "apply"
	}
	if err := opts.validate(); err != nil {
		return result, err
	}
	repositoryID, err := rest.RepositoryID(ctx, opts.Org, opts.Repository)
	if err != nil {
		return result, enrollError{code: "repository_unavailable"}
	}
	groups, err := rest.RunnerGroups(ctx, opts.Org)
	if err != nil {
		return result, enrollError{code: "runner_groups_unavailable"}
	}
	group := &groupResult{Name: opts.RunnerGroup, SelectedWorkflows: opts.Workflows}
	result.RunnerGroup = group
	setResult := &scaleSetResult{Name: opts.ScaleSet, Labels: opts.Labels}
	result.ScaleSet = setResult

	var existing *runnerGroup
	for index := range groups {
		if groups[index].Name == opts.RunnerGroup {
			if existing != nil {
				return result, enrollError{code: "runner_group_ambiguous"}
			}
			existing = &groups[index]
		}
	}

	if existing == nil {
		group.Action = "create"
		if !opts.Apply {
			setResult.Action = "create"
			return result, nil
		}
		created, err := rest.CreateRunnerGroup(ctx, opts.Org, createRunnerGroup{
			Name:                     opts.RunnerGroup,
			Visibility:               "selected",
			SelectedRepositoryIDs:    []int64{repositoryID},
			AllowsPublicRepositories: false,
			RestrictedToWorkflows:    true,
			SelectedWorkflows:        opts.Workflows,
		})
		if err != nil || created.ID <= 0 || created.Name != opts.RunnerGroup {
			return result, enrollError{code: "runner_group_create_failed", next: "rerun the same command: it re-observes before creating"}
		}
		existing = &created
		group.ID = &created.ID
	} else {
		group.ID = &existing.ID
		repositories, err := rest.RunnerGroupRepositoryIDs(ctx, opts.Org, existing.ID)
		if err != nil {
			return result, enrollError{code: "runner_group_repositories_unavailable"}
		}
		if existing.Default || existing.Visibility != "selected" || existing.AllowsPublicRepositories ||
			!slices.Equal(repositories, []int64{repositoryID}) {
			return result, enrollError{code: "runner_group_conflict", next: "a group with this name exists with another shape; pass --runner-group glaeda-linux-trusted"}
		}
		current := sortedUnique(existing.SelectedWorkflows)
		switch {
		case existing.RestrictedToWorkflows && slices.Equal(current, opts.Workflows):
			group.Action = "exists"
		case !opts.UpdateWorkflows:
			group.Action = "workflows_differ"
			group.PreviousWorkflows = current
			return result, enrollError{code: "runner_group_workflows_differ", next: "rerun with --update-workflows to replace the group's workflow list"}
		default:
			group.Action = "update_workflows"
			group.PreviousWorkflows = current
			if opts.Apply {
				updated, err := rest.UpdateRunnerGroupWorkflows(ctx, opts.Org, existing.ID, opts.Workflows)
				if err != nil || updated.ID != existing.ID || !updated.RestrictedToWorkflows ||
					!slices.Equal(sortedUnique(updated.SelectedWorkflows), opts.Workflows) {
					return result, enrollError{code: "runner_group_update_failed"}
				}
			}
		}
	}

	actionsGroupID, err := actions.RunnerGroupID(ctx, opts.RunnerGroup)
	if err != nil {
		return result, enrollError{code: "actions_runner_group_unavailable"}
	}
	group.ActionsID = &actionsGroupID
	set, err := actions.ScaleSet(ctx, actionsGroupID, opts.ScaleSet)
	if err != nil {
		return result, enrollError{code: "scale_set_unavailable"}
	}
	if set == nil {
		setResult.Action = "create"
		if !opts.Apply {
			return result, nil
		}
		labels := make([]scaleset.Label, 0, len(opts.Labels))
		for _, label := range opts.Labels {
			labels = append(labels, scaleset.Label{Name: label, Type: "System"})
		}
		if _, err := actions.CreateScaleSet(ctx, &scaleset.RunnerScaleSet{
			Name:          opts.ScaleSet,
			RunnerGroupID: actionsGroupID,
			Labels:        labels,
			RunnerSetting: scaleset.RunnerSetting{DisableUpdate: true},
		}); err != nil {
			return result, enrollError{code: "scale_set_create_failed", next: "rerun the same command: it re-observes before creating"}
		}
		// Fresh post-effect observation: report only what GitHub now holds.
		set, err = actions.ScaleSet(ctx, actionsGroupID, opts.ScaleSet)
		if err != nil || set == nil {
			return result, enrollError{code: "scale_set_unobserved_after_create"}
		}
	} else {
		setResult.Action = "exists"
	}
	if err := checkScaleSet(set, opts, actionsGroupID); err != nil {
		return result, err
	}
	setResult.ID = &set.ID
	if !opts.Apply && group.Action != "exists" {
		return result, nil
	}
	result.Enrollment = &enrollmentResult{
		ScaleSetID:    set.ID,
		ScaleSetName:  set.Name,
		RunnerGroupID: actionsGroupID,
		Labels:        opts.Labels,
		Owner:         opts.Org,
		Repository:    opts.Repository,
	}
	return result, nil
}

// checkScaleSet holds the same identity the bridge's start validation requires.
func checkScaleSet(set *scaleset.RunnerScaleSet, opts options, groupID int) error {
	labels := make([]string, 0, len(set.Labels))
	for _, label := range set.Labels {
		labels = append(labels, label.Name)
	}
	if set.ID <= 0 || set.Name != opts.ScaleSet || set.RunnerGroupID != groupID || !set.RunnerSetting.DisableUpdate ||
		!slices.Equal(sortedUnique(labels), opts.Labels) || len(labels) != len(opts.Labels) {
		return enrollError{code: "scale_set_conflict", next: fmt.Sprintf("scale set %q exists with another shape; choose another --scale-set name", opts.ScaleSet)}
	}
	return nil
}

func errorCode(err error) (string, string) {
	var typed enrollError
	if errors.As(err, &typed) {
		return typed.code, typed.next
	}
	return "enroll_failed", ""
}
