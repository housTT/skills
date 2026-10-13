# Google Drive upload

Used by the `upload-op-analysis` skill for one run that `validate_static.py` passes. Only when the
host lists Google Drive and Google Sheets connector tools; otherwise say the CSVs are in
`<run dir>` and stop.

The spreadsheet is a view of the run for people, built entirely by `sheet_spec.py`: Info (run
details, counts as formulas over the Op table, `summary.md`, and the op-table columns that hold
one value in every row), Kernel ops (one row per kernel op, evidence de-duplicated across its
rows), Op table (one row per launching variant, device op and factory merged, constant columns
dropped, source paths shortened), then Kernel tests, Call trace, Port blockers, Host ops and any
other CSV of the run. Never assemble a tab's values by hand.

1. Ask for the Drive folder only when `run.json` has no `drive_url` (link or name). Create a
   subfolder named after the model if the user wants one. With a `drive_url`, say which
   spreadsheet will be refreshed and skip the folder.
2. One spreadsheet per run. Without a `drive_url` in `run.json`, create one named
   `<Model> kernel op analysis (<impl-slug> on <source>, <sha7>)`; its first tab has id 0. With a
   `drive_url` (a destination or measurement was added since the last upload), reuse that
   spreadsheet: read its tabs, and in the batch of step 3 first delete every tab but the first
   and clear the first (`updateCells` on its whole range with `fields: "*"`).
3. Run `python3 <plugin-root>/scripts/sheet_spec.py <run dir> --info-sheet-id <first tab id>
   --first-sheet-id <highest id the spreadsheet has used + 1>` and save its JSON. Send its
   `requests` in one batch update: they rename the first tab to Info, add the other tabs in order
   and format them (wrapped, top-aligned cells, fixed widths, bold frozen header rows). Do not
   autosize columns; long evidence must wrap, not widen the sheet.
4. Write each entry of `tabs` with one `update_formulas` call on `'<title>'!A1` with its `values`
   as given: formulas stay formulas, numbers stay numbers, and text the sheet would parse already
   carries a leading `'`.
5. Sheet row = CSV `id` + 1 on the Kernel ops, Op table and CSV tabs (header is row 1). Before any
   targeted cell write, read the rows and confirm the `id` column; after writing, read the range
   again and check it.
6. Read back every cell in `expected` and compare it with the value given there; then read the Op
   table back and compare it with the Op table entry of `tabs`. A mismatch is an upload error:
   fix and re-check before reporting. Record the URL in `run.json` (`drive_url`) and link it in
   the reply.
