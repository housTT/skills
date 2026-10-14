# setup-box: Exabox

An exabox is a Slurm cluster of whole Tenstorrent boards. Its site entry uses route `slurm`, and
every rule and command in `slurm.md` applies. This page adds what is particular to it.

## How it is set up

- Partitions are exclusive (`OverSubscribe=EXCLUSIVE`): a job gets a whole node. Nodes report
  placeholder CPU and memory and no GRES, so chips are not requested: the partition's board type
  decides them.
- The site file maps each partition to its chips and board. Check it with `tt-smi -ls` inside the
  first job on each partition.
- One-chip work: an exclusive node of the smallest board type, limited to one chip with
  `--devices 0` (`TT_VISIBLE_DEVICES`) or the test's mesh setting. Four-chip work: a node of a
  4-chip board type, or 4 chips of a larger node (`--devices 0,1,2,3`).
- Accounting may be disabled: `sacct` then does not work. Read status from `squeue` and
  `scontrol show job` while the job lives, and from its `job.rc` and `job.log` afterwards
  (`box.py slurm status`).
- The default time limit may be short and the maximum unlimited: always pass `--time` as the
  expected run time plus a small margin. Preemption may cancel a job: resubmit it.
- Node-local disk, `/tmp` and `/dev/shm` may be wiped around each job. Keep checkouts, builds,
  logs and rc files on the shared storage the site file names (`workdir_root`); the login node's
  home may be local to it.

## Etiquette

- Get nodes only through `sbatch` (or `srun` for one command), as one self-ending batch job.
  Never `salloc --no-shell` or any allocation held for later steps.
- Never take a node that is allocated, drained, reserved (for maintenance, demos or others) or
  down. `box.py slurm check` reads the node's state and the reservations, and the site
  file's `demo_nodes` and `excluded_nodes`.
- The job script starts and kills every process itself; never start work by ssh into a node.
- Release the node when the work ends, record the allocation (cluster, job id, node, start, end,
  purpose) with `ttp note`, and run `box.py slurm verify-clean`.
- A project's charter may narrow these rules (which nodes, how long a node must have been idle):
  pass them as `--allow` and `--min-idle`.

## Reaching the login node

- The login route may run through a tunnel from the user's laptop, which is down while the laptop
  sleeps or is offline. Then ssh exits 255.
- Do not wait or retry in a loop: hand off `waiting` with a `retry_when` that probes ssh, for
  example `ssh -o BatchMode=yes -o ConnectTimeout=15 <login-alias> true`. A job already submitted
  keeps running; its `retry_when` also returns 255 (still asleep) until the route is back.
