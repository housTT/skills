---
"tt-project": patch
---

`tt-project`: the worker hook refuses the web token path after any character, closing the
backslash, glob, command-substitution, brace and backtick bypasses.
