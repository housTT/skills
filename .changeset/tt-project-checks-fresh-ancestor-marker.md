---
"tt-project": patch
---

`ttp checks --fresh` ignores an inherited verified-through marker at an ancestor of HEAD too, not only one at HEAD, so every check runs again as its message says.
