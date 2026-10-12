---
"tt-project": patch
---

Add `ttp deployed --after-push`: an owned template merge with a takeover deadline is pending rather than a deploy failure. The deploying project emits one low note and keeps durable, model-free checks due five minutes after takeover. Missed takeovers and other verification failures have project/release condition keys that clear when the running daemon reports the release or a newer version. Ordinary `ttp deployed` remains a strict read-only probe. Pending checks alert a stopped daemon or a current upgrade error at the next probe, ignore upgrade errors from older releases or other commits, follow a new commit of the same version, and let a recovered daemon return to pending; alert deduplication no longer decides the outcome.
