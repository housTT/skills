---
"tt-project": patch
---

`tt-project`: The worker system prompt now carries only the rules its task needs. Shared-cluster rules
appear only when the charter's Resources or a machine it names is a cluster. Device rules appear only
for device projects, hosts or tasks. Device-runner (`ttp devq`) rules appear only when runners are
configured. Remote-driver rules appear only when a task names a machine or a runner is remote.
Detailed waiting rules (drivers, `on_pass_cmd`, wake tiers) are left out for kinds that cannot wait
(question, plan). PR delivery rules go only to code and review tasks. Harness-only rules go only to
harness tasks. The safety rules every task needs stay. The rules a code task reads on every call drop
from 20.5k to about 12.6k chars, and a question task's to about 9.7k. A test caps their size.
