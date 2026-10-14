---
"tt-project": patch
---

Shared-cluster rules in the worker and coordinator prompts, the charter template and the create guide now state only general scheduler etiquette: get nodes through the scheduler, never take one allocated, drained or reserved for others, run self-ending batch jobs sized to the run, release when done and log every allocation. They no longer carry a default node list or idle threshold; a project that wants one writes it in its own charter, and a task stays within the nodes, partitions or limits its charter names.

Other one-user or one-site content is now generic: the coordinator's unblocking rule describes a staged rollout; the device runner's `legacy_driver` setting is `outside_user` (an outside device user to wait for; the old key is still read); examples use neutral model names, file names, ports and UTC times; the ssh auth-failure hint names a reboot as one possible cause; maintainer-only notes moved to docs/design.md. The `plugins/<name>/` manifest-version guard in `ttp push` and the push queue is now `delivery.plugin_version_guard`, off by default. The leak test now also covers the plugin's tests.
