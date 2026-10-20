# Resource locks

`ttp lock <resource> -- <cmd>` holds one slot while a command runs.
`--shared` (and `--probe --shared` in a `retry_when`) is for read-only commands: shared holders run together, exclusive holders (a plain `ttp lock`, an exclusive task's whole-run hold) and shared ones exclude each other, and a queued exclusive waiter is not jumped by later shared ones.
Never on a device lock (in `device.locks` or ending in `-device`): it exits 2 and runs nothing.
A nested `ttp lock` of a lock already held in the same or a stronger mode (or inside an exclusive task's run) just runs; an exclusive one inside a shared hold of the same lock exits 2 at once.
The worker prompt names only the plain form (its size cap); `ttp lock --help` has the rest.
