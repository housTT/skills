---
"tt-project": patch
---

`ttp machines change add|edit --until-task <id>`: a shared-machine ledger entry (such as a staged build
tree a follow-up task needs) stays open while that task of the project is open and is overdue once it is
done, failed, cancelled or gone (at the latest after 7 days). A continuation task re-extends it with its
own id; the daily review lists open entries whose task is over.
