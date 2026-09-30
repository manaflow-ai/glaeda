# macOS desktop-test VM on a fleet mini

This is the checked-in control contract for the optional desktop-test VM. It is
one long-lived macOS guest per eligible M4 Pro mini, started only when a GUI
job is waiting and the host has headroom. It does not build products and it
does not contain a warm checkout. E2E and app-host jobs receive the product
artifact from CI in the same way as the existing reuse path.

The candidate pilot backend is [Lume](https://github.com/trycua/cua), subject to
an image receipt that does not exist yet. Lume is intended to use Apple's
Virtualization.framework, support unattended macOS guests with a native display,
and is MIT licensed. Tart is not a runtime
dependency. The host adapter is [`scripts/glaeda-macos-vm`](../scripts/glaeda-macos-vm):
`plan` is read-only. `apply` is deliberately disabled: immutable provider
ownership, a runner admission drain barrier, serialized reconciliation, and an
isolated guest-to-host accounting protocol have not yet been proven. A missing
tool, unknown VM identity, unknown job activity, stale inventory, or failed
post-action observation blocks planning.

The target base image would be macOS 26.5 or newer with the pinned Xcode
command-line tools, the Actions runner, the Glaeda hook, the UI-test account,
and reviewed Accessibility, Screen Recording, and Automation grants. No base
image, grant verification, provider receipt, or break-glass clone exists yet.
The target VM profile is 8 vCPUs, 16 GiB RAM, and an 80 GiB sparse disk. It
would be long-lived and never reset or cloned per job.

The intended guest runner uses the same GUI label as the mini's host GUI runner,
so the CI route remains GUI-only. Its intended `gui-vm` accounting and the host
`gui` token still need a host-side broker or another verified isolation boundary;
the guest must not receive read-write access to the host capacity ledger. No
runner enrollment, image creation, launchd service, checkout, credential
directory, runner work tree, or product cache is installed by this PR.

The proposed elastic policy uses a one-second `top` sample and the macOS VM
pressure level. Its pressure thresholds, queue signal, drain protocol, and
serialized start/stop controller remain design inputs only. No guest job is
admitted, stopped, suspended, or resumed by this PR, and an offline runner is
not treated as proof that a job is waiting.

## Pilot gate

The disk gate is evaluated before image creation and before every start. The
pilot begins on cmux10s, then cmux11s and cmux12s only after each has at least
120 GiB free. For each mini, record the exact Glaeda and Lume image receipts,
guest macOS/Xcode versions, host pressure samples, job admission decisions,
pass rate, p50/p95 duration, and host CPU and memory pressure. Compare the
same E2E/app-host legs on the host GUI runner and the VM runner for several
hours. A timing-sensitive Metal, lag, or recorded-tour case remains host-only
until it passes the same acceptance probe in the guest.

Rollback and runner enrollment are future work after the ownership and drain
protocol are accepted. Deleting a VM is a separate, explicit recovery action
and is not part of this planning surface.
