---
"tt-project": patch
---

A worker run that failed, spent nothing and whose typed error says the API rejected the request (an
HTTP 400, an invalid request or an invalid schema: Claude's assistant `error` event or failed result,
Codex's failed turn, Cursor's failed result) now ends as `request_error`. Its task fails at once
instead of retrying, and a later retry is not raised a tier. Stderr text alone, a priced run, a run
with output, and account errors (auth, limits, funds) never count; each provider's auth and limit
classification is unchanged. A failed resume still starts fresh. A coordinator turn that ends this
way keeps the usual failed-turn backoff. Known gap: a rejection is acted on only when the run ends;
nothing stops a run mid-way on one.
