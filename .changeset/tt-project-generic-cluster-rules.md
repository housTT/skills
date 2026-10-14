---
"tt-project": patch
---

Shared-cluster rules in the worker and coordinator prompts, the charter template and the create guide now state only general scheduler etiquette: get nodes through the scheduler, never take one allocated, drained or reserved for others, run self-ending batch jobs sized to the run, release when done and log every allocation. They no longer carry a default node list or idle threshold; a project that wants one writes it in its own charter, and a task stays within the nodes, partitions or limits its charter names.
