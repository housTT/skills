---
"tt-project": patch
---

Probe exit codes: `tt-box-clean-probe` now exits 75 (not 255) when it cannot tell, and a probe
255 whose host answers (or names no host) is a broken probe, not an outage. `setup-box` bare
`check` exits 255 only for ssh's own 255; a timeout or failed remote probe exits 1.
