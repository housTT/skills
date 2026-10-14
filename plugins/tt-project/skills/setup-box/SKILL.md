---
name: setup-box
description: "Get a Tenstorrent device box and work on it over ssh: pick a route (bare metal, IRD reservation, Slurm or exabox), keep a checkout or git worktree on the box, edit, build and run there, and report host, path, commit and job ids back. Use when a tt-project task needs a device box, or the user asks to set one up."
---

# tt-project: setup-box

## Purpose

- The project box only coordinates. Device work happens on the device box, over ssh.
- This skill gets a box, sets up a checkout there, and says what a box task reports back.
- Site values (aliases, paths, partitions, pools) live in the local site file, never in a repo:
  `site.md`.

## When to Invoke

- A task needs a device box, or a box it used is gone.
- The user asks to set up, reserve or allocate a box.
- A project still syncs or mirrors files to a box: `migrate-off-sync.md`.

## Route picker

Read the box's entry in `~/.tt-project/boxes.json` (by its `ttp machines` alias) and check it
with `<plugin-root>/skills/setup-box/scripts/box.py site check`. Its `route` picks the page.

| You have | Route | Page |
|---|---|---|
| A machine you can ssh to and own for now | `bare` | `bare-metal.md` |
| A reservation system with a management host | `ird` | `ird.md` |
| A Slurm cluster (1-chip, 4-chip or whole-node jobs) | `slurm` | `slurm.md` |
| A Slurm cluster with whole-node exclusive partitions | `slurm` | `exabox.md` |
| No entry yet | ask the user for the route and aliases, write the entry | `site.md` |

All routes share `ssh-workflow.md` (aliases, jump host, detached jobs) and `agents.md` (agent CLIs
and build/run recipes on the box).

## Etiquette on shared machines

- Get a box through its scheduler or reservation system; never ssh into a node you hold no
  allocation or reservation for.
- Never take a node or box that is allocated, drained, reserved or down, and never cancel, join or
  share another user's allocation.
- Reserve or allocate only for the run, release right after it, and re-reserve when needed. Never
  auto-extend a reservation, and hold nothing idle.
- A project's charter can narrow these rules (which machines, how long); it never widens them.

## Work on the box

- The device task ssh-es to the box and edits, builds and runs in its own checkout or
  `git worktree` there (`ssh-workflow.md` names and cleans them).
- It commits and pushes from the box, or reports a diff or commit id.
- Never sync, mirror or copy the project box's files to the box (no rsync, no sshfs, no scp of
  sources). The repository is the only channel.
- Long work runs detached on the box with a log and an rc file (`ssh-workflow.md`); the task hands
  off `waiting` with a `retry_when` that reads the rc file.

## Storage

- `workdir_root` (site file): shared storage for checkouts and build trees that must outlive a job.
- `scratch_root` (site file): node-local scratch, fast but wiped when a job ends on some clusters.
  Copy results you keep back to `workdir_root` before the job ends.
- Never assume a path: take both from the site file, and check free space with `df <path>`.

## Hand-off from a box task

A box task's result.json says:

| Field | What |
|---|---|
| host | the `ttp machines` alias (never a raw hostname) |
| path | the checkout or worktree path on the box |
| commit | the commit it built or ran, and whether it was pushed (branch) or only reported (diff) |
| jobs | job or reservation ids it started, with start and end |
| still running | what is still running or held on the box, or "nothing" after checking |
