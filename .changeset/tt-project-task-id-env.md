---
"tt-project": patch
---

`tt-project`: every run, relaunched check and model-free `on_pass_cmd` now also exports the task's id as `TTP_TASK_ID` (same value as `TTP_TASK`; empty for the coordinator), so a spec command such as `--task "$TTP_TASK_ID"` no longer gets an empty argument. Push queues, deploys and push-check children drop or carry it exactly as they do `TTP_TASK`.
