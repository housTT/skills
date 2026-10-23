# Schedule budgets and skips

- `budget_usd` (`budget_usd_day` in `schedules.json`) caps a schedule's spend over 24 h.
- `budget_usd_run` (llm payload) caps one run. Unset, it is the tier's `budget.task_default_usd`, never the whole daily cap. A run never gets more than what is left of the day.
- Every skipped trigger, whatever the reason (daily budget, debounce, precheck, gate, open run), is kept in the schedule's skip history with its time and reason: the last 50, kept across daemon restarts. `ttp schedules <name> show <schedule>` lists them; the schedule list and the web app count the last 24 h.
- A skip for the schedule's own daily cap while the gate allows optional work raises one `daemon:schedule-budget` observation per schedule a day, closed once the schedule runs again. Expected skips (debounce, a precheck exit 0, a yellow, orange or red gate) are only logged.
