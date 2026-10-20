# Own pushes

`delivery.own_push`: `on`, `existing_only` (also unset) or `off`.
`existing_only` publishes only onto a branch the remote already has.
Unset, a code task may still create its own `ttp/t<id>-...` branch (draft PRs on); an explicit `existing_only` never creates one.
A code task refused creating another task's branch it carries is told its route: commit on its own branch and run `ttp push --own-branch --detach`.
Never `git push` around a refusal.

## Hand-off lint

Model-free, run by the daemon on every code hand-off (no worker call).
Where a code task may publish its own branch (`own_push` `on`, or unset with draft PRs), a follow-up proposing to push that branch while it is not on the remote is flagged as a task note, and the daemon queues no hop for it.
Publish with `ttp push --own-branch --detach` before handing off.
