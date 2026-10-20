# Memory tidy-up

`ttp memory <name> --forget <entry> <entry> ... [--why W]` retires several entries in one call: one MEMORY.md rewrite and one harness commit, far faster than one call per entry.
A name it cannot retire (unknown, ambiguous, standing without `--why`) is reported and the command exits non-zero; the others are still retired.
To merge overlapping entries use `ttp memory <name> "<merged>" --kind K --supersedes <entry>...` (one commit; refused whole if it would drop a standing flag, change a user rule's kind or lose the user's quoted words).
`--search <words>` finds live and archived entries.
Decisions and facts no prompt has shown for `memory.archive_hidden_days` days (default 7) are archived daily, model-free.
