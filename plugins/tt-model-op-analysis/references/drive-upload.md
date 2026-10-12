# Google Drive upload

Used by the `upload-op-analysis` skill for one run that `validate_static.py` passes. Only when the
host lists Google Drive and Google Sheets connector tools; otherwise say the CSVs are in
`<run dir>` and stop.

1. Ask for the Drive folder only when `run.json` has no `drive_url` (link or name). Create a
   subfolder named after the model if the user wants one. With a `drive_url`, say which
   spreadsheet will be refreshed and skip the folder.
2. One spreadsheet per run. Without a `drive_url` in `run.json`, create one named
   `<Model> kernel op analysis (<impl-slug> on <source>, <sha7>)`. With a `drive_url` (a
   destination or measurement was added since the last upload), reuse that spreadsheet: delete
   every tab but the first and clear the first, then in step 3 pass `--summary-sheet-id <first tab
   id> --first-sheet-id <highest id the spreadsheet has used + 1>`. `changes_<table>.csv` files
   from `compare_runs.py` become `Changes: <tab>` tabs.
3. Run `python3 <plugin-root>/scripts/sheet_spec.py <run dir>`. Send its `requests` in one batch
   update: it renames the first tab to `Summary`, adds one tab per CSV in a fixed order with a
   readable title, wraps and top-aligns every cell, sets the column widths and freezes and bolds
   the header row. Do not autosize columns; long evidence must wrap, not widen the sheet.
4. Write each table with one call from its top-left cell: its `headers` from the spec as row 1
   (readable names, not the CSV keys), then the CSV rows. Numbers stay numbers; prefix text that
   the sheet would parse as a number, date or formula with `'`.
5. Summary tab, from `summary.md`: the kernel-op table first, written from `kernel_ops.csv` with
   the headers that `sheet_spec.py` lists under `summary_tables`; then label in column A, text in
   column B, one row per item or bullet; tables (launch totals, status counts) as small grids. Totals are formulas over the
   tables (`=SUM(...)`, `=SUMPRODUCT(...)`, `=COUNTIF(...)`), not typed numbers, and must equal
   what the validator printed.
6. Sheet row = CSV `id` + 1 (header is row 1). Before any targeted cell write, read the rows and
   confirm the `id` column; after writing, read the range again and check it.
7. Read back the totals and status counts and compare them with `summary.md`. Record the URL in
   `run.json` (`drive_url`) and link it in the reply.
