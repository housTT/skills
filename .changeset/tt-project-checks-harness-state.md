---
"tt-project": patch
---

`ttp checks --purpose harness` no longer refuses ("commit first") over unstaged edits to harness state no worker's change owns: `*.jsonl` records, files under a `state/` directory, and in the project's own harness repo its `project.json`, `schedules.json`, charter and memory. It lists them. The worker's own uncommitted or staged changes still block, and the candidate purpose is unchanged.
