---
"tt-project": patch
---

`tt-project`: a wait on another project's reply is no longer refused when its `retry_when` is the
project's own model-free wrapper: an `ack_wait.py probe` under the project's harness dir, or a command
listed in the new `ack_wait.probe_commands` setting (matched on its leading words). Any other command is
still refused, and the refusal now names the setting.
