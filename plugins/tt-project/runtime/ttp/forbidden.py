"""Forbidden-data patterns: text a project must never write into its harness git (memory entries,
charter items and their commit messages), for projects whose charter keeps their data out of git.

A project declares them, all optional, none by default:
- a charter section whose heading starts with "Forbidden data": each backticked span on its lines is
  a regex, the words before it on a "- label: `regex`" line its label;
- project.json `memory.forbidden_patterns`: regex strings, or {"name": ..., "regex": ...} tables;
- project.json `memory.forbidden_patterns_file`: a file (relative to the project folder; keep it
  outside the harness, e.g. under state/) with one regex per line, `#` comments allowed. Use it for
  patterns that would themselves reveal the data, such as names: it is never committed.

Regexes use Python syntax; add `(?i)` for any case. A refusal names the pattern, never the text."""
from __future__ import annotations

import re
from pathlib import Path

SECTION = "forbidden data"
_TICKS = re.compile(r"`([^`]+)`")


def is_section(heading: str) -> bool:
    """Whether a charter heading (with or without its `## `) is the forbidden-data section."""
    return heading.lstrip("#").strip().lower().startswith(SECTION)


def declared(p) -> list[tuple[str, str]]:
    """(label, regex) for every pattern the project declares, in charter, config, file order."""
    from .prompts import charter_sections
    out: list[tuple[str, str]] = []
    try:
        charter = p.charter_path.read_text()
    except OSError:
        charter = ""
    for heading, body in charter_sections(charter):
        if not is_section(heading):
            continue
        for line in body:
            for rx in _TICKS.findall(line):
                label = line.split("`", 1)[0].strip().lstrip("-*").strip().rstrip(":").strip()
                out.append((label or f"charter Forbidden data item {len(out) + 1}", rx))
    mem = p.config().get("memory") or {}
    for i, item in enumerate(mem.get("forbidden_patterns") or []):
        if isinstance(item, dict):
            out.append((str(item.get("name") or f"memory.forbidden_patterns[{i}]"), str(item.get("regex") or "")))
        else:
            out.append((f"memory.forbidden_patterns[{i}]", str(item)))
    if name := mem.get("forbidden_patterns_file"):
        path = Path(name) if Path(name).is_absolute() else Path(p.base) / name
        try:
            lines = path.read_text().splitlines()
        except OSError as e:
            # Fail closed: a configured list that cannot be read must not let the data through.
            raise ValueError(f"memory.forbidden_patterns_file {name} cannot be read ({e.strerror}): "
                             "nothing written; fix or unset it") from None
        n = 0
        for line in lines:
            if line.strip() and not line.lstrip().startswith("#"):
                n += 1
                out.append((f"{path.name} line pattern {n}", line.strip()))
    return [(label, rx) for label, rx in out if rx.strip()]


def matches(p, *texts: str) -> list[str]:
    """Labels of the declared patterns any of `texts` matches. An invalid regex raises ValueError
    (fail closed): a pattern the user meant to apply must not silently stop applying."""
    hits = []
    for label, rx in declared(p):
        try:
            pat = re.compile(rx)
        except re.error as e:
            raise ValueError(f"forbidden-data pattern {label!r} is not a valid regex ({e}): nothing written; "
                             "fix it in the charter or config") from None
        if any(t and pat.search(t) for t in texts) and label not in hits:
            hits.append(label)
    return hits


def check(p, what: str, *texts: str) -> None:
    """Refuse (ValueError) text matching a declared pattern, before anything is written or committed.
    The message names the patterns, never the matched text, so logs and digests do not carry it."""
    hits = matches(p, *texts)
    if hits:
        raise ValueError(f"{what} refused: the text matches the project's forbidden-data pattern"
                         f"{'s' if len(hits) > 1 else ''} {', '.join(repr(h) for h in hits)}; nothing was written "
                         "or committed. Keep the data in its work file and write a pointer to that file "
                         "(its path, no figures, names or numbers) instead")
