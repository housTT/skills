---
"tt-project": patch
---

New probe `ttp broker-released` for a `retry_when` or `on_pass_cmd` that waits on a device broker's hold: it reads /health JSON and log text (files or stdin) and exits 0 once /health shows `held` false with a healthy fsm, or the log's last hold line is `hold released` (`STARTUP hold released ...`) or a `device_released` event; 1 while not. A live /health that is still held wins over an older release line. The worker prompt, `operate.md` and the harness skill point broker-release waits at it, so a check no longer misses a release the broker reported another way.
