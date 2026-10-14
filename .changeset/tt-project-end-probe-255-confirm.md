---
"tt-project": patch
---

`tt-project`: An `end_when` or `until_probe` exit 255 (a resource pause, an observation mute, a temporary memory or charter entry) now counts as "not yet" only when ssh cannot reach any host the probe names, the same check `start_when` and `retry_when` probes get. A probe that names no host, or whose host answers, is reported as broken. The ssh check runs in the background, so the daemon never waits on it.
