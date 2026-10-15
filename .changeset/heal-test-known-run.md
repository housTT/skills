---
"tt-project": patch
---

`ttp heal test` reads a check result like the daemon: exit 75 or 1 with a
`ttp-heal: known <owner>: <why>` line prints `known (<owner>: <why>)`; without
the line it still prints unknown.
