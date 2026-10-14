# setup-box: Slurm

Other people share the cluster's nodes. Hold a node only while work runs on it.

## Rules

- Get nodes only through the scheduler (`sbatch`, or `srun` for one command). Never ssh into a
  node you hold no allocation for, and never start work on one that way.
- Never take a node that is allocated, drained, reserved for others or down, and never cancel,
  preempt, join or share another user's allocation.
- Submit the whole run as one batch job that ends itself, with `--time` set to the expected run
  time plus a small margin, never the partition maximum. No interactive or no-shell allocation
  (`salloc --no-shell`) kept for later steps, and no job that waits on another task, a review, a
  tunnel, a laptop or a human.
- The job script starts every process it needs and kills them all before it exits, so releasing
  depends on nothing outside the cluster.
- Hold nothing idle: an editor, `tmux` or agent session on a node is not using it. Release the
  node as soon as the work ends, and check afterwards that nothing of yours still runs there.
- A project's charter may narrow these rules (which nodes or partitions, how long a node must have
  been idle); `box.py slurm` takes those as `--allow` and `--min-idle`. Without them it applies
  only the rules above.

## How the cluster tells you what is free

| Need | Command (on the login node) |
|---|---|
| Partitions, their nodes and states | `sinfo -N -o '%N %P %T'` |
| One node: state, last busy time, reason | `scontrol -o show node <node>` (`State=`, `Reason=`, last busy time) |
| Reservations (maintenance, demos, other teams) | `scontrol -o show reservation` |
| Limits: default and maximum time, preemption | `scontrol show partition <partition>` (`DefaultTime`, `MaxTime`, `PreemptMode`) |
| Your jobs | `squeue -u "$USER"` |

- Take a node only when its state is plain `IDLE`. `MIXED`, `ALLOCATED`, `COMPLETING`, `PLANNED`
  or any `+DRAIN`, `+RESERVED`, `+MAINT` or `+NOT_RESPONDING` flag means it is not free, and so
  does a suffix such as `IDLE*` (not responding), `IDLE~` (powered down) or `IDLE$` (maintenance).
- A node listed in any reservation is not free, even when it shows `IDLE`.
- A default time limit may be short (often an hour); always pass `--time`.
- With `PreemptMode=CANCEL` a higher-priority job may end yours; resubmit it.

### Last busy time

`scontrol show node` gives the last time Slurm saw the node busy. Many unrelated nodes sharing one
last busy time, to the second, usually means a controller event (a restart or reconfigure), not
jobs. A `--min-idle` check against it is then conservative (it may refuse a node that has been free
longer), never unsafe. Where accounting is on, `sacct -N <node>` shows when the last real job on
the node ended.

## Chips: one-chip and four-chip work

How chips are allocated depends on the cluster:

- With GRES for chips (`scontrol show node` shows `Gres=`), request them as the cluster documents.
- Without GRES (`Gres=(null)`), and with nodes reporting placeholder CPU and memory, the unit of
  allocation is a whole node: use `--exclusive`. The chip count is then a property of the node's
  board type and partition, not something a job requests.

| Work | Whole-node cluster |
|---|---|
| One chip | an exclusive node of the smallest board type, with `TT_VISIBLE_DEVICES=0` (or the test's mesh setting) limiting the run to one chip |
| Four chips | a node of a 4-chip board type, or 4 chips of a larger node (`TT_VISIBLE_DEVICES=0,1,2,3` or the test's mesh setting) |
| A whole board | a node of that board type, no device limit |

The site file maps each partition to its chips and board (`site.md`). On a new cluster the chip
count is not known until a job measures it: leave `chips` out or set it to `"unknown"`. `site check`
then warns instead of failing, and `check` and `submit` still work. A job on such a partition first
writes the number of devices in `/dev/tenstorrent` to `job.chips` and logs `tt-smi -ls`; `status`
prints the count once the job ended. Record it in the site file. A card with two chips behind one
device (such as n300) counts once, so read the `tt-smi -ls` output in `job.log` before recording.
Verify a recorded mapping the same way in the first job on each partition, and fix the site file
if it differs.

## box.py slurm

`<plugin-root>/skills/setup-box/scripts/box.py slurm ...` runs every Slurm command on the box's
login node (`slurm.login`) over ssh.

| Command | What |
|---|---|
| `survey <box> [--partition P] [--min-idle MIN]` | read-only (`sinfo` and `scontrol` only, never `sbatch`, `srun` or `salloc`): per partition (default: the site file's), node counts by state, every plain `IDLE` node with its last busy time, minutes since then and why it is not takeable (a reservation, `demo_nodes`, `excluded_nodes`, under `--min-idle` when given), and the reservations on its nodes; ends with a note for each last busy time that several nodes share |
| `check <box> <node> [--partition P]` | exit 0 only when the node is plain `IDLE`, in no reservation, not in the site file's `demo_nodes` or `excluded_nodes`, and in partition P; also checks `--allow` and `--min-idle` (minutes since the node was last busy) when given |
| `submit <box> --node N --partition P --time T --name NAME --checkout PATH --purpose TEXT --command CMD [--devices 0]` | runs `check`, writes `job.sbatch` under `<workdir_root>/runs/<NAME>` and submits it with `sbatch --parsable --exclusive -w N -p P --time=T`; prints the job, a `note:` line and a `retry_when:` line; on a partition without a measured chip count the job also measures it |
| `status <box> <job> [--name NAME]` | the job's state from `squeue` while it lives; after it ends, its `job.rc`, the measured chip count when the job took one, and the last 20 lines of `job.log`; exit 2 when `squeue` fails (state unknown: never treat that as ended) |
| `verify-clean <box> <node> [--ssh]` | exit 0 when no job of yours is on the node; with `--ssh`, also lists your processes there |

`survey` takes no threshold of its own: an idle minimum is site or project policy, passed as
`--min-idle` only when the charter sets one. Use it to pick a node, then `check` or `submit` it: the
survey is a snapshot, and `check` runs again right before `sbatch`.

`submit` refuses a missing or unlimited `--time`, `salloc` and `--no-shell`, a checkout or run dir
outside `workdir_root`, a partition missing from the site file, and any node `check` refuses.

The job script it writes:

- runs in its own process group, `cd`s into the checkout on shared storage and runs the command
  (with `TT_VISIBLE_DEVICES` when `--devices` is given);
- traps `EXIT`, `TERM`, `INT` and `HUP`: it kills its whole process group, then writes the exit
  code to `job.rc`; the output goes to `job.log`.

After submitting:

1. Run the printed `note:` line (cluster, job id, node, start, limit, purpose), and list the job in
   the hand-off.
2. Hand off `waiting` with the printed `retry_when`: it exits 0 once `squeue` no longer lists the
   job, 1 while it does or `squeue` fails, 255 while ssh fails.
3. On waking, `box.py slurm status <box> <job> --name NAME`. A time limit or preemption sends TERM,
   which the trap records as `rc 143`. `rc missing` means a SIGKILL before the trap ran, a node
   failure, or a job that never started: quote the log tail and resubmit or report it. Exit 2
   means `squeue` failed and the job may still run: retry later, never resubmit on it.
4. `box.py slurm verify-clean <box> <node>`, and record the end time with `ttp note`.

## Status after a job ends

- Where accounting is enabled, `sacct -j <job>` gives the final state. Where it is disabled
  (`scontrol show config` shows `AccountingStorageType=accounting_storage/none`), `sacct` does not
  work: the job's own `job.rc` and `job.log` are the only record once `squeue` drops it.
- `scontrol show job <job>` works only while the job is alive or shortly after.

## Storage

- Keep the checkout, build tree, `job.log` and `job.rc` on shared storage (`workdir_root`).
- A node's local disk may be wiped when a job starts or ends (Prolog/Epilog), including your files
  in `/tmp` and `/dev/shm`. Use node-local scratch (`scratch_root`) only for data the job copies
  back to `workdir_root` before it ends.
- The login node's home may be its own local disk, not the shared storage the nodes see.

## Checking a node afterwards

- On many clusters ssh to a compute node is allowed only during your own allocation. Then
  `verify-clean` can check only that no job of yours remains; it says it cannot check processes.
  Say so in the hand-off.
- Where ssh to nodes is always allowed, `verify-clean --ssh` also lists your processes there.
