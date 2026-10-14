# setup-box: IRD reservations

IRD reserves a Tenstorrent machine for a set time and gives you ssh into a container on it.
IRD may be deprecated: prefer Slurm where your site has it (`slurm.md`).

## Reach

- IRD is reachable only from networks that can reach the IRD service (e.g. the corporate VPN).
- `ird` runs only on the management host. Inside a reservation container it allows only
  `list-machines`, `run` and `sync`, so every call goes from the project box:
  `ssh -n -o BatchMode=yes <ird-mgmt-alias> ird ...`.
- `box.py ird` checks first that the management host's ssh hop has a DNS name and answers on its
  ssh port. If not, it says so and exits 255 instead of hanging; run it from a host that can
  reach IRD.

## Site entry

```json
"<box-alias>": {
  "route": "ird",
  "workdir_root": "/<workdir>",
  "ird": {"mgmt": "<ird-mgmt-alias>", "pools": ["<pool>"], "user": "<your-ird-user>"}
}
```

`ird.mgmt` is the management host's ssh alias, `ird.pools` the pools you may reserve from (first
is the default). `ird.user` is optional: without it `box.py` asks the management host (`id -un`).
`excluded_nodes` and `demo_nodes` are never offered or reserved. See `site.md`.

## Reserve only for the run

Reserve, use, release right after, and re-reserve when needed. Never auto-extend, and never hold
a machine idle between runs.

```
S=<plugin-root>/skills/setup-box/scripts/box.py
$S ird list <box-alias>                 # machines you can reserve now (--all: skipped ones and why)
$S ird reserve <box-alias> --arch <arch> --timeout <hh:mm:ss> [--pool <pool>] [--machine <m>] [--ssh-port <p>]
$S ird status <box-alias>               # your reservations, their claims and expiry
$S ird release <box-alias> <id>         # right after the run
```

- `list` skips machines that are draining, down, failed or pending a reboot (`Reboot ASAP`), and
  busy ones (`alloc`).
- `reserve` runs `ird reserve --cluster <pool> --timeout <hh:mm:ss> [--machine <m>]
  [--ssh-port <p>] --no-shell <arch>`. Every flag goes before the arch: some `ird` builds reject a
  flag after it and silently never reserve. `box.py` then checks that a new reservation of yours
  appears, and fails if none does. Reserves sharing a state dir run one at a time. If several
  new reservations of yours match the request (another task reserved at once), it claims none and
  says so: check `status`, retry with `--machine`, and release what you do not use.
- `--wait` (default 270 s) caps how long `ird reserve` may take. On a timeout the reservation may
  still land: check `$S ird status <box-alias>` before reserving again.
- Set `--timeout` to the run's expected length plus a small margin.
- It prints `reserved <id> machine=<m> port=<p> expires=<time>`. Put the id and expiry in the
  task's hand-off (`jobs` in SKILL.md), and release before the hand-off unless the run continues.
- A run that turns out longer: `$S ird extend <box-alias> <id> <minutes>` (`ird change-timeout`),
  on demand from that task only, never from a daemon, hook or timer.

## ssh to the reservation

`reserve` writes one block per reservation to `~/.tt-project/ird/ssh-hosts`
(`$TTP_IRD_DIR/ssh-hosts`), and `release` removes it:

```
Host <machine>
    HostName <machine>
    Port <port>
    ProxyJump <ird-mgmt-alias>
```

- `~/.ssh/config` needs `Include ~/.tt-project/ird/ssh-hosts` at its top. `reserve` says when it
  is missing: ask the user to add it (`ssh-workflow.md`); never edit their ssh config yourself.
- A machine you never reached has no host key in `known_hosts`, so `BatchMode` refuses it. The
  user verifies it once (`ssh-workflow.md`); never turn host key checking off.
- Then work as on any box: `ssh <machine>`, checkout or worktree under `workdir_root`
  (`ssh-workflow.md`). Never sync or mirror files to it.

## Ownership and claims

- `box.py ird` acts only on reservations whose user column in `ird list` is yours. It never
  releases, extends or reboots anyone else's.
- `reserve` writes a claim per reservation in `~/.tt-project/ird/claims/`: project
  (`$TTP_PROJECT`), task (`$TTP_TASK`), owner pid (the run's `runner.pid`) and a heartbeat.
- `release <id>`, `extend` and `reboot` refuse a reservation with no claim (made by hand or
  elsewhere), one claimed by another project, and one with a live claim of another task. A claim
  is live while its heartbeat is newer than `--ttl` (default 1 h) or its owner pid still runs.
- `release --stale [--dry-run]` releases only your own reservations with a stale claim of this
  project (left by a run that died), and drops claims whose reservation has ended. It skips the
  rest and says why. Release the others by hand if they are yours.

## Unwedge a machine

First triage and recover the device with tt-buddy's `skills/run/recovery.md` (triage, kill,
reset, verify). When it says a host reboot is needed:

1. `$S ird reboot <box-alias> <id>`: runs `ird reboot <id>`, which only schedules a drain-gated
   reboot that fires once the reservation ends, then releases it. `ird reboot --force` does
   nothing inside a container; do not use it.
2. Wait until `$S ird list <box-alias>` shows the machine free again (a few minutes), from a
   `retry_when`, not a sleep loop.
3. Re-reserve the same machine and port, as `reboot` prints:
   `$S ird reserve <box-alias> --arch <arch> --machine <m> --ssh-port <p> --timeout <hh:mm:ss>`.
   The ssh block keeps working.

If the machine does not come back, or the device still fails after the reboot, release and
reserve another machine.

## Exit codes

0 done; 1 refused or failed (the line says why); 255 the management host is unreachable.
