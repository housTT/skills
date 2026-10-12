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
   every tab but the first and clear the first, then in step 3 pass `--info-sheet-id <first tab
   id> --first-sheet-id <highest id the spreadsheet has used + 1>`. `changes_<table>.csv` files
   from `compare_runs.py` become `Changes: <tab>` tabs.
3. Run `python3 <plugin-root>/scripts/sheet_spec.py <run dir>`. Send its `requests` in one batch
   update (on a refresh, the tab deletions and the clear go first in the same batch): it renames
   the first tab to `Info`, adds `Summary` as the second tab and one tab per CSV after it in a
   fixed order with a readable title, wraps and top-aligns every cell, sets the column widths and
   freezes and bolds the CSV header rows. Its `info` and `summary` entries give those tabs'
   sheetIds. Do not autosize columns; long evidence must wrap, not widen the sheet.
4. Write each table with one call from its top-left cell: its `headers` from the spec as row 1
   (readable names, not the CSV keys), then the CSV rows. Numbers stay numbers; prefix text that
   the sheet would parse as a number, date or formula with `'`.
5. Info tab (first), the explanation a reader needs before the tables; label in column A, text in
   column B, one row per item:
   - the `summary.md` title, then the run from `run.json`: model, implementation, entry test,
     source and its `profile_values`, destinations, tt-metal commit with ref and date, TTsim
     commit when set, and the entry test's CI evidence;
   - then all of `summary.md` except its kernel-op table: each heading as a bold row in column A;
     a `label: text` bullet as label and text; other bullets, paragraphs and code-block lines in
     column B; numbered items with their number in column A. Drop Markdown markup (`**`, backticks).
   Summary tab (second): the kernel-op table from row 1, written from `kernel_ops.csv` with the
   headers that `sheet_spec.py` lists under `summary_tables` as a bold grey header row, and a bold
   `Total` row under it; then
   small grids with bold header rows: launches per inference (maximum, unconditional minimum,
   conditional launch rows, call-trace total, kernel-op total) and status counts per profile column
   of the op table (one row per status, then a total); then one row pointing to the Info tab.
   Totals are formulas over the tables (`=SUM(...)`, `=SUMIFS(...)`, `=SUMPRODUCT(...)`,
   `=COUNTIF(...)`), not typed numbers, and must equal what the validator printed.
6. Sheet row = CSV `id` + 1 (header is row 1). Before any targeted cell write, read the rows and
   confirm the `id` column; after writing, read the range again and check it.
7. Read back the totals and status counts and compare them with `summary.md`. Record the URL in
   `run.json` (`drive_url`) and link it in the reply.
