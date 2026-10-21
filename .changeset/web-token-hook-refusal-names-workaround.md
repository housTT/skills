---
"tt-project": patch
---

The worker hook's web token refusal now says the workaround (`git commit -F <file>`,
or call it "the web token file"), and also refuses the path after `=` and similar
separators (`--file=<path>`). Regression tests pin every known bypass shape.
