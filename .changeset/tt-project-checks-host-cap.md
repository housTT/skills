---
"tt-project": patch
---

A host-wide cap on concurrent check runs: `ttp checks`, `ttp push`, `ttp push --own` and the push queue's checks each take one of a few slots shared by every project of the account on the host (default one per 16 cores; `ttp config --account checks.host_max N`, or `TTP_CHECKS_HOST_MAX`; 0 turns it off). Runs over the cap wait their turn in arrival order without failing; the wait shows in the run's notes, the `--result` probe and `ttp status`, and does not count toward a run's wall clock. A check nested in one that holds a slot runs at once. Load-sensitive subprocess tests scale their timeouts with the host's load (or `TTP_TEST_TIMEOUT_SCALE`).
