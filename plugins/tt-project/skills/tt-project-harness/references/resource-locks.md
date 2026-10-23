# Resource locks

`ttp lock <resource> -- <cmd>` holds one slot while a command runs.
`ttp lock A,B -- <cmd>` (the names in one word) holds several, all or nothing: it keeps its arrival place in every queue, holds none of them until it can take them all (in name order, so two such lockers never deadlock), and lets all go when the command ends. A `ttp lock` run under another hold (an enclosing `ttp lock` or the task's whole-run hold) does not wait behind it, so the two never wait for each other. `ttp lock --probe A,B` exits 75 when any is busy or paused; `--json` lists each one's holders.
`--shared` (and `--probe --shared` in a `retry_when`) is for read-only commands: shared holders run together, exclusive holders (a plain `ttp lock`, an exclusive task's whole-run hold) and shared ones exclude each other, and a queued exclusive waiter is not jumped by later shared ones.
Never on a device lock (in `device.locks` or ending in `-device`): it exits 2 and runs nothing.
A nested `ttp lock` of a lock already held in the same or a stronger mode (or inside an exclusive task's run) just runs; an exclusive one inside a shared hold of the same lock exits 2 at once.
The worker prompt names only the plain form (its size cap); `ttp lock --help` has the rest.
