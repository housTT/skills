# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Run an immutable check definition, with coverage separate from publication identity."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from . import push

GIT_ERROR, IDENTITY_ERROR, INPUT_ERROR = 20, 65, 66
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class PlanError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)


def _read_git(repo: Path, *args: str) -> bytes:
    try:
        result = _git(repo, *args)
    except OSError as e:
        raise PlanError(GIT_ERROR, f"git unavailable: {e}") from e
    if result.returncode:
        raise PlanError(GIT_ERROR, f"git failed ({result.returncode}): {result.stderr!r}")
    return result.stdout


def _path(path) -> bool:
    return (isinstance(path, str) and bool(path) and not path.startswith(("/", "~"))
            and all(p not in ("", ".", "..", ".git") for p in path.split("/"))
            and not any(c in path for c in "\x00\n\r"))


def _blob(repo: Path, commit: str, path: str) -> bytes:
    # Literal pathspec: a coverage path never acts as a glob or pathspec directive.
    listing = _read_git(repo, "ls-tree", "-z", commit, "--", f":(literal){path}")
    if not listing:
        raise PlanError(INPUT_ERROR, f"missing required committed input: {path}")
    mode, kind, rest = listing.split(b" ", 2)
    if mode not in (b"100644", b"100755") or kind != b"blob":
        raise PlanError(INPUT_ERROR, f"required input is not a regular file: {path}")
    oid = rest.split(b"\t", 1)[0].decode("ascii")
    return _read_git(repo, "cat-file", "blob", oid)


def _scope(scope) -> dict:
    if not isinstance(scope, dict) or set(scope) != {"paths", "commands"}:
        raise PlanError(INPUT_ERROR, "coverage needs exactly paths and commands")
    paths, commands = scope["paths"], scope["commands"]
    if not isinstance(paths, dict) or not paths or any(
            not _path(p) or not isinstance(h, str) or not _DIGEST.fullmatch(h) for p, h in paths.items()):
        raise PlanError(INPUT_ERROR, "paths must bind exact repository files to SHA-256 digests")
    if not isinstance(commands, list) or not commands or any(
            not isinstance(c, str) or not c.strip() or push.check_problem(c) for c in commands):
        raise PlanError(INPUT_ERROR, "commands must be a nonempty list of check commands")
    return scope


def _verify(repo: Path, head: str, scope: dict) -> None:
    for path, expected in scope["paths"].items():
        blob = _blob(repo, head, path)
        if (digest := hashlib.sha256(blob).hexdigest()) != expected:
            raise PlanError(IDENTITY_ERROR, f"uncovered committed input: {path} (sha256 at HEAD: {digest})")
        file = repo / path
        try:
            if not file.resolve().is_relative_to(repo.resolve()) or file.is_symlink():
                raise PlanError(INPUT_ERROR, f"required input is not a local regular file: {path}")
            if file.read_bytes() != blob:
                raise PlanError(IDENTITY_ERROR, f"working input differs from committed coverage: {path}")
        except OSError as e:
            raise PlanError(INPUT_ERROR, f"missing or unreadable working input: {path}") from e


def run(repo: Path, definition_commit: str | None, definition_path: str, purpose: str) -> int:
    """No skips or pass cache. Exit 65 identity/coverage, 66 input/definition, 20 git;
    selected commands retain their exit code. Purpose is explicit, never inferred from HEAD.
    A `None` definition commit reads a harness definition from HEAD's own commit (never the working
    copy), so a change to covered inputs and its refreshed digests land in one commit, with no
    second commit to move a pin."""
    try:
        at_head = definition_commit is None
        if at_head and purpose != "harness":
            raise PlanError(INPUT_ERROR, "only a harness definition can be read at HEAD; candidate "
                            "identities need a pinned definition commit")
        if not (at_head or _OID.fullmatch(definition_commit)) or not _path(definition_path):
            raise PlanError(INPUT_ERROR, "definition needs a full commit ID and exact repository path")
        # Paths are repository-relative: run from the top level even when started in a subdirectory.
        repo = Path(_read_git(repo, "rev-parse", "--show-toplevel").decode().strip())
        head = _read_git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        if at_head:
            definition_commit = head
        try:
            found = _git(repo, "cat-file", "-e", f"{definition_commit}^{{commit}}")
        except OSError as e:
            raise PlanError(GIT_ERROR, f"git unavailable: {e}") from e
        if found.returncode:
            raise PlanError(INPUT_ERROR, f"definition commit {definition_commit} is not in repository {repo}; "
                                         "a purpose harness check runs only in the harness repo")
        resolved = _read_git(repo, "rev-parse", "--verify", f"{definition_commit}^{{commit}}").decode().strip()
        if resolved != definition_commit:
            raise PlanError(IDENTITY_ERROR, "definition commit did not resolve exactly")
        try:
            plan = json.loads(_blob(repo, definition_commit, definition_path))
        except (ValueError, UnicodeError) as e:
            raise PlanError(INPUT_ERROR, "invalid check definition JSON") from e
        if not isinstance(plan, dict) or plan.get("purpose") not in ("harness", "candidate"):
            raise PlanError(INPUT_ERROR, "definition purpose must be harness or candidate")
        if plan["purpose"] != purpose:
            raise PlanError(IDENTITY_ERROR, "check purpose differs from the committed definition")
        if purpose == "candidate":
            candidates = plan.get("candidates")
            if set(plan) != {"purpose", "candidates"} or not isinstance(candidates, dict) or any(
                    not _OID.fullmatch(h) for h in candidates):
                raise PlanError(INPUT_ERROR, "candidate definition needs full commit identities")
            for value in candidates.values():
                _scope(value)
            if head not in candidates:
                raise PlanError(IDENTITY_ERROR, f"Unreviewed delivery identity: {head}")
            scope = candidates[head]
        else:
            if set(plan) != {"purpose", "coverage"}:
                raise PlanError(INPUT_ERROR, "harness definition needs coverage")
            scope = _scope(plan["coverage"])
        _verify(repo, head, scope)
        print(f"check-plan: {purpose} on {head}; definition {definition_commit}:{definition_path}"
              + (" (at HEAD)" if at_head else ""), flush=True)
        for command in scope["commands"]:
            print(f"$ {command}", flush=True)
            rc = subprocess.run(push.check_argv(command, repo), cwd=repo).returncode
            if rc:
                return rc if rc > 0 else 128 - rc
            if _read_git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip() != head:
                raise PlanError(IDENTITY_ERROR, "check changed HEAD")
            _verify(repo, head, scope)
        return 0
    except PlanError as e:
        print(f"check-plan: {e}", file=sys.stderr)
        return e.code


def command(args) -> None:
    raise SystemExit(run(Path.cwd(), None if args.definition_at_head else args.definition_commit,
                         args.definition_path, args.purpose))
