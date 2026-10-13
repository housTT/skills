---
"tt-project": patch
---

`tt-project`: upstream notes forwarded to another machine now close too. Each `ttp upstream --receive`
answer carries back the latest resolution of every note received from the sender. The sender's
forwarder records these locally, so its project's `ttp upstream --fixed <fp>` passes and its
coordinator is told once. The sender's `--ack` goes back with its next send and closes the note on
the maintainers' machine. A sender that still has open forwarded notes asks at most hourly with an
empty send. A receiver on an older release gets no acks and is asked only at the 6-hour probe
interval.
