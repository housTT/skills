---
"tt-project": patch
---

`tt-project`:

- The install command is one short line, `curl -fsSL <raw install.sh URL> | sh`; `install.sh` now
  holds every check (git, Python 3.9+), the `TT_PROJECT_REF`/`TT_PROJECT_REPO` options and safe
  reruns. To install a release tag, name it in the URL and in `TT_PROJECT_REF`. A downloaded
  `install.sh` run on its own fetches the ref like the piped line. README and install docs trimmed.
