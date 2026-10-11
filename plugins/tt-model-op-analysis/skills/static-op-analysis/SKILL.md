---
name: static-op-analysis
description: Build the kernel-op table for a tt-metal model test by reading code at a pinned commit, tracing every ttnn call to its device op and program factory for P100, P150 and Quasar profiles, with validator-checked counts and CSV output. Use for "op table", "which kernel ops does model X use", or a kernel op support table with Blackhole and Quasar status.
---

# Static op analysis

Reads code only; never touches a device. Every count must survive
`<plugin-root>/scripts/validate_static.py`, and every dispatch claim carries evidence.

`<plugin-root>` is the `tt-model-op-analysis` plugin directory, two levels above this
SKILL.md; resolve every `<plugin-root>/...` path against it.

## Steps

1. **Setup.** Follow the static order in `<plugin-root>/references/run-setup.md`: model, target
   architecture/profile, pinned ref, compatible implementation, ranked test candidate, output
   directory and overwrite/new/merge. Architecture is selected before implementation and test.
2. **Targets.** Load `<plugin-root>/references/targets.md`, confirm the selected profile values at
   the pinned SHA, and write `targets` and `profile_values` to `run.json`.
3. **Inventory.** Read the model file and the test once, by range. List every ttnn call on the
   forward path (call site, stage, how many times it runs per inference), including calls that
   become views, equality no-ops or untaken branches, and every predicate from targets.md the code
   reads. Note host-side work for `host_ops.csv`. Keep zero-launch decisions in the inventory and
   summary/call-trace notes, not as device-op rows in `op_table.csv`.
4. **Trace.** Load `<plugin-root>/references/dispatch-tracing.md`. Small model: trace yourself.
   Larger model: one subagent per block with the subagent contract in that file. Collect rows for
   `op_table.csv` and `call_trace.csv` per `<plugin-root>/references/schemas.md`, then normalize
   duplicate call sites and zero-launch decisions yourself before writing either table.
5. **Quasar.** If `quasar` is a target, load `<plugin-root>/references/quasar-status.md` and fill
   the three Quasar status columns (as written, port, bf16 projection) and `quasar_blockers.csv`.
6. **Re-check conservatively.** Audit every `verified` row and every definitive `✅` or `❌` claim,
   not only the uncertain rows. `verified` requires the complete dispatch chain for the exact
   variant. A Quasar `✅` requires exact matching coverage; a `❌` requires a directly applicable
   failing check or result. When evidence is incomplete, indirect or only similar, use
   `unverified`/`⚠️` and name the missing fact. Apply the same status consistently to rows sharing
   the relevant dtype/layout/factory path unless row-specific evidence distinguishes them.
7. **Gate.** Run `python3 <plugin-root>/scripts/validate_static.py <run dir>`. Fix and rerun until
   it prints `OK`. No output, summary or upload before that.
8. **Summary.** Write `summary.md`: model, test, SHA, profiles, launches per inference per profile
   (copy the validator-checked totals), count of each status per column, top Quasar blockers,
   open `unverified` items. For a merge, add the changes section from run-setup.md. End with a
   "How to read this" section: every column of every CSV and every status value used, in plain
   words, including what the bf16 projection assumes.
9. **Upload.** Ask whether to upload. If yes, follow `<plugin-root>/references/drive-upload.md`.

## Rules

- Read sources as `git show <sha>:<path>` with line ranges; never the working tree. The only
  exception is an approved temporary test in the run directory (run-setup.md, section 1).
- In steps 3-6, read no file of an earlier run under `op-analysis/`: an earlier run is not
  evidence. Only `compare_runs.py` opens one, in the merge step.
- Write CSVs with a script or a heredoc into the run directory; never retype a table to fix it.
- A status of `✅` needs evidence a reader can open. When unsure, `⚠️` with the reason.
- Prefer an explicit uncertainty over an inferred definitive claim. Do not upgrade confidence or
  status because a likely branch, familiar operation name or analogous test looks convincing.
- Do not edit the tt-metal checkout.
- Report what the validator checked and what stayed `unverified`; do not claim a device run.
- `validate_static.py` is a structural/count gate, not evidence that a factory choice or support
  status is correct. The manual re-check in step 6 remains mandatory after it prints `OK`.
- Output stands alone: a reader who knows no issue, ticket, tracker or earlier conversation must
  understand every file. Never refer to criteria, items or numbers defined elsewhere ("criterion
  3", "4b"); state what is meant instead. A link may cite a source, never replace a definition.
