---
"tt-project": patch
---

`tt-project`: `ttp push --own` no longer runs the project's checks again when this tree already passed
them in `ttp checks`. `ttp checks` now records each command's environment with its pass (the interpreter
and venv it uses, the active venv, and the pytest-related variables `PYTHONPATH`, `PYTEST_ADDOPTS`,
`PYTEST_PLUGINS`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD`). `ttp push --own` reuses a pass on exactly HEAD's tree
with the same commands and environment, says so, and takes no checks slot. A changed tree, command or
environment, or a pass recorded before this change, still runs them. The push branch and the push queue
still run their own checks.
