---
name: upload-op-analysis
description: Upload the local results of a tt-model-op-analysis run (kernel-op tables, summary, measurements) to a Google Sheets spreadsheet in Google Drive. Asks where the analysis results are, lists the model's runs, checks the chosen run, then asks for the Drive folder and writes one formatted spreadsheet per run, refreshing the same spreadsheet on later uploads. Use for "upload the op analysis", "put the ResNet50 kernel-op analysis on Drive", or refreshing a run's sheet after new measurements.
---

# Upload op analysis

Sends one or more finished runs of the `model-op-analysis` skill from the local disk to Google
Sheets. It never analyses, traces or measures anything; it only reads a run directory.

`<plugin-root>` is the `tt-model-op-analysis` plugin directory, two levels above this SKILL.md;
resolve every `<plugin-root>/...` path against it.

Needs the Google Drive and Google Sheets connector tools. If the host does not list them, say which
connector is missing and stop; the results stay in the run directories.

## Flow

Ask one decision per turn, recommended option first; never infer choices from open files, IDE
state or earlier conversations.

1. **Output root.** Ask where the analysis results are: the root that holds `op-analysis/`
   (the same root the analysis skill wrote to). State it instead of asking when the user's
   instructions fix it.
2. **Model.** List the model directories under `<root>/op-analysis/` and ask which one.
3. **Run.** Run `python3 <plugin-root>/scripts/runs.py list <root> <model>` and show each run:
   implementation, source, commit, destinations, pending measurements and whether it was uploaded
   (`drive_url`). Ask which run to upload; several may be picked. Old-format and unreadable runs
   cannot be uploaded; say why.
4. **Check.** For each picked run, `python3 <plugin-root>/scripts/validate_static.py <run dir>` must
   print `OK`, and `kernel_ops.csv` and `summary.md` must exist. Otherwise say what is missing and
   that the `model-op-analysis` skill finishes the run; skip that run.
5. **Drive folder.** Only for runs without a `drive_url`: ask for the Drive folder (link or name).
   A run with a `drive_url` refreshes its existing spreadsheet; say which.
6. **Upload.** For each run, follow `<plugin-root>/references/drive-upload.md`. It records the
   spreadsheet URL in `run.json` (`drive_url`).
7. **Report.** One line per run: the spreadsheet link, created or refreshed, and the totals read
   back from the sheet against `summary.md`.

## Rules

- Never edit the run's CSVs or `summary.md`; the sheet is a copy of them.
- Counts and totals in the sheet come from the CSVs and the validator output, never retyped.
- Sheet row = CSV `id` + 1; read the rows back before and after any targeted write.
- Never trash or delete a Drive file the user did not name.
