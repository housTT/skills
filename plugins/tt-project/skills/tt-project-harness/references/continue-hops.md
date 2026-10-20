# Continue hops

A done task's only follow-up titled `continue: ...` is queued by the daemon at the parent's tier (caps under `chains`).
A hop that only runs checks, pushes or edits spec or PR text costs less at light: title it `mechanical: ...`, or give the follow-up `"tier": "light"` (lower only, never above the parent's).
Its spec is unchanged; a light hop that needs judgment is retried a tier up.
