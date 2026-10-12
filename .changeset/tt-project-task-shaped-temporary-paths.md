---
"tt-project": patch
---

Device-job lint excludes configured temporary-directory ancestors when comparing task ids,
while preserving warnings for job ids and task paths beneath scratch. Regression checks cover
task-shaped temporary paths and check output after a daemon restart without an inherited run directory.
