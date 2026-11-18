// Command scaleset-enroll is the one-shot, explicit enrollment for Glaeda's trusted Linux Runner
// Scale Set. The long-running bridge refuses to create a scale set; this command creates (or adopts,
// when the shape matches exactly) one org runner group restricted to one repository and to workflows
// pinned on protected base branches, plus one scale set in it, then prints the identity the
// disposable-worker enrollment needs. It plans by default; --apply mutates. It never deletes.
//
// Credentials: --token-file names the installation token file that the App key's home host pushes
// (mode 0600, owned by this user). The App private key never reaches this host.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"io"
	"os"
	"os/signal"
	"strings"
	"time"
)

const defaultAppID = 5067688

type listFlag []string

func (values *listFlag) String() string { return strings.Join(*values, ",") }
func (values *listFlag) Set(value string) error {
	*values = append(*values, value)
	return nil
}

func main() {
	os.Exit(mainWithArgs(os.Args[1:], os.Stdout, os.Stderr))
}

func mainWithArgs(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("scaleset-enroll", flag.ContinueOnError)
	flags.SetOutput(stderr)
	tokenPath := flags.String("token-file", "", "absolute path of the pushed installation token JSON (mode 0600)")
	appID := flags.Int64("app-id", defaultAppID, "GitHub App ID the token file must name")
	opts := options{}
	flags.StringVar(&opts.Org, "org", "manaflow-ai", "organization")
	flags.StringVar(&opts.Repository, "repository", "cmux", "the one repository the runner group admits")
	flags.StringVar(&opts.RunnerGroup, "runner-group", "cmux-fleet-linux", "org runner group name")
	flags.StringVar(&opts.ScaleSet, "scale-set", "glaeda-linux-arm64", "scale set name")
	var labels, workflows listFlag
	flags.Var(&labels, "label", "scale set label (repeatable; default glaeda-linux-arm64)")
	flags.Var(&workflows, "workflow", "allowed workflow ORG/REPO/.github/workflows/FILE.yml@refs/heads/{main,feat-cmux-next} (repeatable)")
	flags.BoolVar(&opts.Apply, "apply", false, "create or update; without it the command only plans")
	flags.BoolVar(&opts.UpdateWorkflows, "update-workflows", false, "replace an existing matching group's workflow list")
	if err := flags.Parse(args); err != nil || flags.NArg() != 0 {
		return 2
	}
	if len(labels) == 0 {
		labels = listFlag{"glaeda-linux-arm64"}
	}
	opts.Labels = labels
	opts.Workflows = workflows

	encoder := json.NewEncoder(stdout)
	encoder.SetIndent("", "  ")
	token, err := readTokenFile(*tokenPath, *appID, time.Now())
	if err != nil {
		_ = encoder.Encode(output{Schema: outputSchema, Mode: modeName(opts.Apply), Org: opts.Org, Error: err.Error()})
		return 1
	}
	actions, err := newOfficialScaleSetAPI(opts.Org, token.value)
	if err != nil {
		_ = encoder.Encode(output{Schema: outputSchema, Mode: modeName(opts.Apply), Org: opts.Org, Error: "scale_set_client_invalid"})
		return 1
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	ctx, cancelTimeout := context.WithTimeout(ctx, 5*time.Minute)
	defer cancelTimeout()
	result, err := run(ctx, opts, newRESTClient("https://api.github.com", token.value), actions)
	if err != nil {
		result.Error, result.Next = errorCode(err)
		_ = encoder.Encode(result)
		return 1
	}
	if encoder.Encode(result) != nil {
		return 1
	}
	return 0
}

func modeName(apply bool) string {
	if apply {
		return "apply"
	}
	return "plan"
}
