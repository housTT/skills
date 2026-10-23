"""API keys for OpenAI-compatible endpoints (`ttp secret endpoint <alias> --url <base url>`).

Each key is saved with its base URL as `endpoint/<alias>` in the per-user secrets store
(project.secrets_path(), mode 0600), next to the jev and slack slots. Only `credentials()` hands the
key back, to code that calls the endpoint; nothing here prints, logs or records it.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request

from .project import load_secrets, save_secret

PREFIX = "endpoint/"
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VERIFY_TIMEOUT_S = 10


def slot(alias: str) -> str:
    """The store key for an alias; ValueError for anything but letters, digits, '-' and '_'."""
    if not ALIAS_RE.match(alias or ""):
        raise ValueError(f"bad endpoint alias {alias!r}: use letters, digits, '-' and '_' only")
    return PREFIX + alias


def base_url(url: str) -> str:
    """The base URL without a trailing slash; ValueError unless it is http(s)://host..."""
    url = (url or "").strip().rstrip("/")
    if not re.match(r"^https?://[^/\s]+", url) or any(c.isspace() for c in url):
        raise ValueError(f"bad endpoint URL {url!r}: give the base URL, e.g. https://api.example.com/v1")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the default handler would resend the Authorization header to the new target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).netloc or "another location"
        raise urllib.error.URLError(f"HTTP {code} redirect to {host}; key not sent")


def verify(url: str, key: str, timeout: float = VERIFY_TIMEOUT_S) -> tuple[bool, str]:
    """GET <url>/models with the key as a bearer token. Returns (ok, message); the message never holds the key."""
    req = urllib.request.Request(f"{url}/models", headers={"Authorization": f"Bearer {key}",
                                                           "Accept": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            return (True, f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code}" + (f" {e.reason}" if e.reason else "")
    except urllib.error.URLError as e:
        msg = str(e.reason) if "redirect to" in str(e.reason) else f"cannot reach {url}: {e.reason}"
    except Exception as e:   # timeouts, bad responses
        msg = f"{type(e).__name__}: {e}"
    return (False, msg.replace(key, "<key>") if key else msg)


def save(alias: str, url: str, key: str, verified: bool, check: str) -> None:
    save_secret(slot(alias), {"url": url, "key": key, "verified": verified, "check": check})


def saved() -> dict[str, dict]:
    """Every saved endpoint by alias (key included: callers must not print it)."""
    return {k[len(PREFIX):]: v for k, v in load_secrets().items()
            if k.startswith(PREFIX) and isinstance(v, dict)}


def show_lines() -> list[str]:
    """One line per saved endpoint for `ttp secret show`: alias, base URL and check state, never the key."""
    out = []
    for alias, v in sorted(saved().items()):
        state = "verified" if v.get("verified") else f"not verified ({v.get('check') or 'no check'})"
        out.append(f"{PREFIX}{alias} {v.get('url', '?')} {state}")
    return out


def credentials(alias: str) -> tuple[str, str] | None:
    """(key, base URL) saved for an alias, or None when none is saved."""
    v = load_secrets().get(slot(alias))
    if not isinstance(v, dict) or not v.get("key") or not v.get("url"):
        return None
    return v["key"], v["url"]
