---
"tt-project": patch
---

The daemon sweeps code tasks left in review: one with no review `review.sweep_after_s` (default 1800, 0 off) after its last run gets its review, model-free, and is not reviewed twice. The sweep only queues reviews; it never closes, cancels or changes a task. Known gap: a task whose continuation already passed review stays in review until the sweep reviews it (one review each, no work lost).
