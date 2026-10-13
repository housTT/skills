---
"tt-project": patch
---

`tt-project`: `ttp machines change edit <id> [--what] [--undo] [--expires | --until-probe]` corrects an
open shared-machine change entry in place instead of closing and re-adding it under a new id. The entry
keeps its id, machine, author and creation time and records who edited it, when and the old values. A
new end drops the old overdue pages. Closed or unknown ids and edits that change nothing are refused.
