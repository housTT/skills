---
"tt-project": patch
---

`ttp say --all [--except p,q] [--context ..] <text>` sends the user's words to every project `ttp list`
shows, once each, under one broadcast id (local projects directly, remote ones over ssh or queued).
A ledger on the machine (~/.tt-project/broadcasts.jsonl) keys the words without case, punctuation or
line breaks: a resend within 24 h reaches only the projects that did not get them yet, and a plain
`ttp say` is recorded too. Each project stores the id (messages.broadcast) and its coordinator digest
marks the message as a broadcast not to relay to other projects. A remote runtime without
`--broadcast` gets the id in the relayer context. `ttp say` stays refused inside runs.
