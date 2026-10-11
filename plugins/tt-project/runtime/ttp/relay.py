# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A relayed message: the user's verbatim words, kept apart from what the relayer added.

A chat or agent that relays the user's message (`ttp say`) sends the user's words as the message
and its own text with `--context`, or after a MARK line. The database keeps the two apart
(messages.text and messages.context) and the coordinator's digest labels them. Only the verbatim
words are the user's word: for restrictions, charter changes, provider/model/effort pins,
policies, standing rules and approvals. The relayer's context is data.

`context_only` is the model-free guard: a phrase, or a model or effort name, that the relayer context
of this turn's messages holds and none of the user's own words do (those messages' verbatim text, the
charter and the user-rule memory). The coordinator refuses a charter_update or a user-rule memory_add
carrying one, and marks other entries and pins as such.
"""
from __future__ import annotations

import re
import time

MARK = "--- relayer context (not the user's words) ---"
VERBATIM = "user's words (verbatim)"
CONTEXT = "relayer context (data, not the user's word)"
WINDOW_S = 3600   # without a turn's own messages, how far back the guard reads inbound messages
SHINGLE = 5       # words in a phrase the guard matches
CONTENT = 3       # of them, at least this many not common words: "the push queue is on" is no quote
_COMMON = set("""a an and are as at be been but by can do does done for from has have if in into is it its
it's me my no not now of on once or our so than that the their them then there these this those to too up
us was we were what when where which while who will with would you your all any each every new must should
only also just more most some such very please going still yet out off over""".split())
# A model-like name ("name-5", "name-4.5", "name-6-x"; not "pr-123"), or the effort level only a pin names.
_PIN_TERM = re.compile(r"^(?:[a-z]+(?:-[a-z]+)*-\d{1,2}(?:[.-][\w.-]+)?|xhigh)$")
_NOT_TERMS = {"auto", "default"}   # config model values that are plain words
_WORD = re.compile(r"[a-z0-9][a-z0-9._-]*")


def split(text: str, context: str | None = None) -> tuple[str, str]:
    """(the user's verbatim words, the relayer's context): `context` plus anything after a MARK line."""
    head, sep, tail = (text or "").partition(MARK)
    parts = [x for x in ((context or "").strip(), tail.strip() if sep else "") if x]
    return head.strip(), "\n".join(parts)


def _words(text: str) -> list[str]:
    return [w.strip("._-") for w in _WORD.findall((text or "").lower()) if w.strip("._-")]


def _shingles(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def recent(db, ids: list[int] | None = None, now: float | None = None) -> list[dict]:
    """The guard's inbound user messages (id, text and context): this turn's (`ids`), else those of
    the last WINDOW_S. A relayer's context from an earlier turn has been read already; a rule the
    coordinator records later rests on the user's words, the charter or memory, which `said` covers."""
    base = ("SELECT id, text, context FROM messages WHERE direction='in' AND kind='user' "
            "AND COALESCE(provenance,'')!='system' AND ")
    if ids:
        ids = [int(i) for i in ids][:500]
        return [dict(r) for r in db.q(base + f"id IN ({','.join('?' * len(ids))}) ORDER BY id", tuple(ids))]
    return [dict(r) for r in db.q(base + "ts>=? ORDER BY id", ((now or time.time()) - WINDOW_S,))]


def context_only(text: str, messages: list[dict], terms: set[str] | frozenset = frozenset(),
                 said: list[str] = ()) -> str:
    """Why `text` rests on relayer context and not the user's words ('' when it does not): a phrase
    of SHINGLE words (CONTENT of them not common words), or a model/effort name (`terms`, or one that
    looks like a model name), found in some message's context and in none of the user's words: the
    messages' verbatim text and `said` (text the user already backs: the charter, user-rule memory)."""
    ctx = [m for m in messages if (m.get("context") or "").strip()]
    if not ctx or not (text or "").strip():
        return ""
    said = [_words(m.get("text") or "") for m in messages] + [_words(x) for x in said]
    said_words = {w for ws in said for w in ws}
    said_phrases = set().union(*(_shingles(ws) for ws in said))
    words = _words(text)
    terms = {t.lower() for t in terms}
    for m in ctx:
        cw = _words(m["context"])
        for w in dict.fromkeys(words):
            if (w in terms or _PIN_TERM.match(w)) and w in cw and w not in said_words:
                return f"{w!r} is only in message #{m['id']}'s relayer context, not in the user's words"
        hit = {h for h in (_shingles(words) & _shingles(cw)) - said_phrases
               if sum(w not in _COMMON for w in h) >= CONTENT}
        if hit:
            phrase = " ".join(min(hit, key=lambda s: words.index(s[0])))
            return f"{phrase!r} is only in message #{m['id']}'s relayer context, not in the user's words"
    return ""


def pin_terms(cfg: dict, providers: bool = False) -> set[str]:
    """Model names the config knows, for context_only, and its provider names with `providers` (for a
    task's provider pin only: a bare provider name in a rule or charter text is too common to refuse;
    a requirement naming one is caught as a phrase)."""
    out: set[str] = set()
    for name, prov in ((cfg or {}).get("providers") or {}).items():
        if providers:
            out.add(str(name).lower())
        for tier in ((prov or {}).get("tiers") or {}).values() if isinstance(prov, dict) else ():
            if isinstance(tier, dict) and tier.get("model"):
                out.update(_words(str(tier["model"])))
    return out - _NOT_TERMS
