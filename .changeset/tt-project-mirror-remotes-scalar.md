---
"tt-project": patch
---

A scalar `delivery.mirror_remotes` (`true`, `5`) is reported by config_problems and refused by config_set ("not a list") instead of silently turning mirroring off.
