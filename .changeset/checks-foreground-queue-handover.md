---
"tt-project": patch
---
`ttp checks` in a tool call no longer blocks for the whole host-wide checks queue: after 60 s with every slot busy it starts the same checks detached, prints their `retry_when` and exits 75. Each check also prints a `running N/M` progress line.
