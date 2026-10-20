---
"tt-project": patch
---

`tt-project`: setup-box sets a new box up in the user's own account, not the shared admin or site login access came through. The new-box questions ask for the login (default: the same name as on the user's other machines), the admin alias, where the uid/gid come from, keys and earlier work to move; `box.py account` creates the account through the admin login with sudo (same uid/gid, home, shell, a `visudo -cf`-checked sudoers drop-in only if the admin has sudo, docker group, authorized_keys, linger; checked as root, never touching another account), moves earlier admin-account work with `mv` and `chown` and lists the venvs and build trees to rebuild, and writes `user`, `admin` and a `workdir_root` under the home into the site entry (`account.md`)
