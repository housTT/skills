---
"tt-project": patch
---

`tt-project`: `ttp setup` now installs the plugin's skills next to the runtime
(`~/.tt-project/lib/<version>/skills/`, also on machines it ships the runtime to), and the new
`ttp skill <name>` prints the path of that release's SKILL.md (`ttp skill` lists them). Workers in any
project, on any agent, can now read the tt-project-harness skill without finding a plugin cache; the
worker prompt names the command.
