---
"tt-project": patch
---

A machine-wide fleet hold: `ttp pause <name> --machine <alias>` (or the coordinator's
`resource_pause` with `machine`) stores one hold that every project on the host honours. Dispatch,
`ttp lock`, status, the web app and `ttp machines list` show and enforce it, a pause of a resource
it covers changes nothing, and only the user lifts a hold they set.
