# Machine change ends

`ttp machines change add ... --until-task <id>` keeps a ledger entry (such as a staged build tree on a shared machine) open while a dependent task of the same project is open.
It is overdue once that task is done, failed, cancelled or gone, and at the latest after 7 days.
A continuation task that still needs it re-extends it as its first step: `ttp machines change edit <cid> --until-task <its own id>`.
The daily review lists open entries whose task is over.

A worker whose sandbox keeps the user's tt-project home read-only still runs `ttp machines change add` and `close` as usual: the CLI then queues the request in the project's own state, and the project's daemon records it within a few minutes (`ttp machines change list` shows it as queued until then).
Running the command again adds nothing. `ttp machines change edit` is not queued (close the entry and add a new one), except `edit <id> --test` or `--no-test`.
