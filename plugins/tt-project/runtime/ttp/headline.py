"""One place that joins a clipped title to its body for alert and feed lines."""
from __future__ import annotations

import re


def clip(text: str, cap: int) -> str:
    """`text` with whitespace normalised, cut at a word boundary with an ellipsis when over `cap`."""
    s = " ".join(str(text or "").split())
    if len(s) <= cap:
        return s
    cut = s[:max(cap - 1, 1)]
    if not s[len(cut)].isspace():
        head = cut.rsplit(" ", 1)[0]
        cut = head if head else cut
    # never end inside an open parenthesis or on a dangling separator
    if cut.count("(") > cut.count(")"):
        cut = cut[:cut.rindex("(")]
    return cut.rstrip(" ,;:-–(") + "…"


def _words(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


def restates(title: str, body: str) -> bool:
    """Whether the body already starts with the title's words (so the title adds nothing)."""
    t, b = _words(title), _words(body)
    if not t or len(b) < len(t) * 0.5:
        return False
    if b[:len(t)] == t:
        return True
    # a title clipped mid-word: its last word is a prefix of the body's word at that place
    k = len(t)
    return k <= len(b) and b[:k - 1] == t[:-1] and b[k - 1].startswith(t[-1])


def join(title: str, body: str, cap: int = 80, prefix: str = "", sep: str = ": ") -> str:
    """`prefix` + title + sep + body; the body alone (after the prefix) when it restates the title,
    and a title over `cap` characters clipped at a word boundary with an ellipsis."""
    t = " ".join(str(title or "").split())
    b = " ".join(str(body or "").split())
    if not t or restates(t[:cap], b) or restates(t, b):
        return f"{prefix}{b}"
    return f"{prefix}{clip(t, cap)}{sep}{b}"
