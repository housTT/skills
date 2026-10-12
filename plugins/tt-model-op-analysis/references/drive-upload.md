# Google Drive upload

Only when the host lists Google Drive and Google Sheets connector tools and the user says yes.
Otherwise say the CSVs are in `<run dir>` and stop.

1. Ask for the Drive folder (link or name). Create a subfolder named after the model if the user
   wants one.
2. New run: create one spreadsheet named
   `<Model> kernel op analysis (<source> to <destination>)`, with `, measured` appended when a
   measured phase ran.
   Overwrite or merge with an existing `drive_url`: reuse that spreadsheet. Delete every tab but
   the first and clear the first, then in step 3 pass `--summary-sheet-id <first tab id>
   --first-sheet-id <highest id the spreadsheet has used + 1>`. A merge run's
   `changes_<table>.csv` files become `Changes: <tab>` tabs.
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
