---
"tt-project": patch
---

The push queue no longer runs a check again on a merged tree where it already passed: a batch retried after being cut short (a reboot) reuses each pass recorded under the same tree, command, interpreter and venv, and target tip, and logs `reused pass from <batch> (tree <short>)`. Failures and checks skipped as not applicable are never recorded. A check that is one plain pytest run also resumes: test files whose every test passed in the cut run are skipped, and only the rest run. Records live in the project's state, bounded and pruned.
