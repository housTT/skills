---
"tt-project": patch
---

Give the coordinator a cache breakpoint on a Claude Code build whose captured requests leave
room, keeping unverified builds conservative. Batch ordinary memory edits by the refresh interval
while applying restrictions immediately. Report zero, partial, full and missing cache telemetry
separately, preserve raw counters, and leave unknown telemetry out of hit and miss rates.
