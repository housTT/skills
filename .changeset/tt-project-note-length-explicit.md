---
"tt-project": patch
---

`tt-project`: `ttp note --to` never cuts a note silently. A note longer than 4,000 characters once
whitespace is joined is refused whole (exit 2, naming the limit and its length), so nothing is filed,
queued or noted. A filed or queued note (inbox, outbox or the run's directory) prints its stored
length and a short sha256, so the sender can check what arrived. A hand-off `upstream:` note or a
daemon page past the limit is cut with a marker saying how much was kept and where the whole text
is. The worker prompt says to put the question or request first.
