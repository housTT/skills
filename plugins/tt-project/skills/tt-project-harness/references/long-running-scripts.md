# Long-running scripts: watcher threads and snapshots

A worker that writes or launches a script meant to run for hours (a driver, a watcher, a monitor) follows these rules.

## A watcher thread

- Guard shared state with a reentrant lock, so a handler that re-enters it cannot deadlock the thread.
- Every lock acquire is bounded; a timed-out acquire records a `stage_timeout` naming the stage, never waits forever.
- A stall guard notices when the thread stops making progress, dumps all thread stacks and exits non-zero, so the hang shows in the log and the exit code instead of a silent stop.

## A script in a shared non-git directory

- It runs from a private snapshot taken at launch: copy the script into a folder of the run's own and run the copy, so an edit to the shared file mid-run cannot change what is running.
- Note the snapshot's full sha256 in the log, so the run can be tied to the exact code it ran.
- Re-exec into a newer version only through a per-script marker the script checks, never by noticing that the shared file changed.
- Snapshot every executable it launches too (wrappers, `setpriv`, `sudo`, a variable naming a further executable); see [watchers.md](watchers.md) for following exec indirection.
- Kernel logs across reboots need `journalctl _TRANSPORT=kernel --since ... --until ...`, not `journalctl -k` (current boot only); see [watchers.md](watchers.md).
- Edit a shared script through a temp file plus `mv`, never in place: overwriting a running script corrupts it.
- Concurrent edits happen only under `ttp lock <name>` or in git.
