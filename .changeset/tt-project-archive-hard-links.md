---
"tt-project": patch
---

The worktree archive stores every small file in full, also when two files share an inode (hard links). Before, the second copy went into the tarball as a link member: the tarball check refused it, so the worktree was kept and retried with an error on every sweep, and restore skipped that copy.
