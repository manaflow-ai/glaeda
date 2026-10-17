# Dev-box disk and Projects hygiene

Glaeda uses one policy surface for fleet minis and developer machines. The role is explicit:

```text
glaeda disk report --role devbox
glaeda disk keepup --role devbox --apply
glaeda projects tidy       # dry run
glaeda projects tidy --apply
```

`fleet-mini` retains the existing cmux hot-tier policy. `devbox` adds safe cleanup for stale
developer worktrees, `/tmp`, Xcode DerivedData, SwiftPM `.build`, Ghostty `.zig-cache`, and
other per-checkout build outputs. Developer worktrees and source checkouts remain report-only;
this policy does not remove user source or Git state. Package caches are pressure-only. Codex transcripts and their
archives remain report-only so Codex task history is preserved. Every candidate is rechecked for open files, a live cwd, command-line use,
socket markers, recent activity, Git state, and submodule state immediately before mutation.

New checkouts go through `~/.local/bin/glaeda-worktree add`. `--no-build` creates the source
worktree without initializing submodules. `--build` initializes Ghostty with `--reference` to
the canonical checkout's module objects. Worktrees live under `~/Projects/worktrees/<repo>/<name>`.

`glaeda projects tidy` reports older linked worktrees, redundant clean pushed clones, and scratch
folders that can move to the canonical layout. It skips live processes, recent paths, and recent
Codex sessions whose cwd names the checkout. Submodule worktrees are status-checked recursively;
if Git refuses to move one, the planner leaves it in place with the metadata and index intact.
The default is report-only;
the compatibility symlink step runs only after every child of a legacy `<repo>-worktrees` folder
has moved successfully.

macOS installs a pressure/idle LaunchAgent and a daily devbox Projects tidy agent. Linux installs
the equivalent user systemd units. Reports and apply receipts remain under the owning user's
Glaeda state directories.
