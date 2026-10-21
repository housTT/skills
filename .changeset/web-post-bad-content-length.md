---
"tt-project": patch
---

Web app: a POST with a non-numeric or negative Content-Length now answers 400
instead of dropping the connection.
