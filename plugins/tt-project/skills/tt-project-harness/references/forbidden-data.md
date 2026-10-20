# Data kept out of git

Three sources declare it: a charter section headed `Forbidden data` (each backticked span on its lines a regex; the words before it on a bullet name it), `memory.forbidden_patterns` (regexes or `{name, regex}`) and `memory.forbidden_patterns_file` (one regex per line, relative to the project folder; keep it under `state/` when the patterns would reveal the data, e.g. names).
`ttp memory`, the coordinator's `memory_add`, `memory_forget` `why` and `charter_update` refuse matching text before writing or committing it; the error names the pattern, never the text.
Declaring any also gives the coordinator its rule to store pointers to work files, not figures.
None are set by default; an unreadable file or invalid regex refuses every write until fixed.
