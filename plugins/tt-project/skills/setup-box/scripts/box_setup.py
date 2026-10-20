"""setup-box new-box flow: `box.py setup questions|save|plan|hostcheck <alias>`.

- `questions` prints what is still to ask about a box: questions never answered, whose wording or
  options changed since they were answered, or whose earlier answer they depend on changed.
  `--format ask` (default) prints batches for an agent's multiple-choice question tool (at most 4
  questions of 2-4 options each); `--format numbered` prints a numbered prompt for agents without
  one; `--all` asks everything again.
- `save` records answers in the box's site entry (`setup`), so a rerun asks only what is new or
  changed. Nothing is filled in by default: an unanswered question stays pending.
- `plan` turns the saved answers into steps, each an action and the check that proves it. It
  refuses while a question is pending. The actions link to tt-buddy and tt-device-mcp's own docs.
- `hostcheck` is one read-only ssh probe: driver, firmware, hugepages, PCIe link.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import tempfile
from typing import Dict, List, Optional

import box

KNOWN_KEYS = {"setup"}
HOST_CHECKS = ("driver", "firmware", "hugepages", "pcie")
AGENT_CLIS = {"claude": "claude --version", "codex": "codex --version", "cursor": "cursor-agent --version"}
TT_PCI_VENDOR = "0x1e52"
DEFAULT_GUARD_GB = 150  # the harness's own disk.min_free_gb default


def _repo(v: str) -> bool:
    parts = v.split("/")
    return len(parts) == 2 and all(p and not p.startswith(".") and all(c.isalnum() or c in "-_." for c in p)
                                   for p in parts)


def _word(v: str) -> bool:
    return bool(v) and not any(c.isspace() for c in v) and not v.startswith("-")


def _names(v: str) -> bool:
    return all(_word(n) for n in v.split(","))


def _gb(v: str) -> bool:
    try:
        return float(v) >= 0
    except ValueError:
        return False


# Each question: id, header (<= 12 chars), text, options [(value, label, description)], suggest
# (offered first and marked, never filled in), multi, other (a validator for a typed answer), when
# (the earlier answers it needs) and parent (whose change makes it ask again).
QUESTIONS: List[dict] = [
    dict(id="broker", header="Broker", text="Device broker on {alias}?", suggest="install", options=[
        ("install", "Install tt-device-mcp", "through its documented installer; device jobs queue through it"),
        ("none", "No broker", "each device command takes `ttp lock {lock} -- <cmd>` instead")]),
    dict(id="broker.ref", header="Broker ref", parent="broker", when={"broker": "install"},
         text="Which tt-device-mcp release or branch?", suggest="latest-release", other=_word, options=[
             ("latest-release", "Latest release", "the newest tagged release"),
             ("main", "main branch", "the default branch; type another tag or branch under Other")]),
    dict(id="host_checks", header="Host checks", multi=True, suggest=list(HOST_CHECKS),
         text="Read-only host checks to run on {alias}? (none: type none)", options=[
             ("driver", "Driver", "the tenstorrent kernel module and its device nodes"),
             ("firmware", "Firmware", "firmware versions the driver reports"),
             ("hugepages", "Hugepages", "1 GB hugepages reserved"),
             ("pcie", "PCIe link", "each card's link speed and width against its maximum")]),
    dict(id="workspace", header="Workspace", suggest="tenstorrent/tt-metal", other=_repo,
         text="A repo checkout and build under the workdir root? (another repo: type owner/name)", options=[
             ("tenstorrent/tt-metal", "tt-metal", "clone and build tenstorrent/tt-metal"),
             ("none", "No workspace", "tasks bring their own checkout")]),
    dict(id="workspace.branch", header="Branch", parent="workspace", when={"workspace": "!none"},
         text="Which branch for the workspace?", suggest="main", other=_word, options=[
             ("main", "main", "the default branch"),
             ("latest-release", "Latest release tag", "the newest tagged release; another branch: type it")]),
    dict(id="workspace.ccache", header="ccache", parent="workspace", when={"workspace": "!none"},
         text="Build with ccache?", suggest="yes", options=[
             ("yes", "Yes", "faster rebuilds; needs ccache installed on the box"),
             ("no", "No", "plain builds")]),
    dict(id="python_env", header="Python env", text="Python env for runs on {alias}?",
         suggest="workspace", options=[
             ("pip-ttnn", "pip ttnn", "a virtualenv with the released ttnn wheel"),
             ("workspace", "Workspace env", "the workspace's own env, built with it"),
             ("none", "None", "tasks set up their own env")]),
    dict(id="agent_clis", header="Agent CLIs", multi=True, suggest=["none"],
         text="Agent CLIs to install on {alias} (only if projects will run there)?", options=[
             ("claude", "Claude Code", "checked with `claude --version`"),
             ("codex", "Codex", "checked with `codex --version`"),
             ("cursor", "Cursor agent", "checked with `cursor-agent --version`"),
             ("none", "None", "agents drive the box over ssh from the project box")]),
    dict(id="projects", header="Projects", suggest="all", other=_names,
         text="Which of your projects may use {alias}? (some: type their names, comma-separated)", options=[
             ("all", "All my projects", "every project whose charter names it"),
             ("none", "None yet", "add it to a project's charter later")]),
    dict(id="recovery.owner", header="Recovery", other=_word,
         text="Which project recovers {alias} when it stays down or held? (type the project name)", options=[
             ("none", "No owner", "you are asked each time")]),
    dict(id="recovery.fallback", header="Fallback", parent="recovery.owner", when={"recovery.owner": "!none"},
         other=_word, text="Which project takes over when the owner cannot act? (type the project name)",
         options=[("none", "No fallback", "you are asked")]),
    dict(id="lock", header="Device lock", suggest="shared", text="One device lock for {alias}, or one per project?",
         options=[
             ("shared", "Shared", "all your projects queue on one `{lock}` lock"),
             ("per-project", "Per project", "each project locks on its own; only for chips split between them")]),
    dict(id="disk_guard", header="Disk guard", suggest=str(DEFAULT_GUARD_GB), other=_gb,
         text="Free-space guard in GB under the workdir root? (another value: type it)", options=[
             (str(DEFAULT_GUARD_GB), f"{DEFAULT_GUARD_GB} GB", "the harness default; enough for a tt-metal build"),
             ("0", "Off", "no guard")]),
]
BY_ID = {q["id"]: q for q in QUESTIONS}


def qhash(q: dict) -> str:
    """Changes when a question's wording, options or condition change."""
    doc = {k: q.get(k) for k in ("text", "options", "multi", "when")}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:12]


def applies(q: dict, answers: dict) -> bool:
    for k, want in (q.get("when") or {}).items():
        got = answers.get(k)
        if got is None:
            return False
        if want.startswith("!") and got == want[1:] or not want.startswith("!") and got != want:
            return False
    return True


def pending(saved: dict, ask_all: bool = False) -> List[dict]:
    """The questions to ask now: those that apply and are new, changed or never answered."""
    answers, asked = saved.get("answers") or {}, saved.get("asked") or {}
    out = []
    for q in QUESTIONS:
        if not applies(q, answers):
            continue
        if ask_all or q["id"] not in answers or asked.get(q["id"]) != qhash(q):
            out.append(q)
    return out


def _fmt(s: str, alias: str, lock: str) -> str:
    return s.replace("{alias}", alias).replace("{lock}", lock)


def render_ask(qs: List[dict], alias: str, lock: str, saved: dict) -> List[dict]:
    """Batches for a multiple-choice question tool; its own free-text choice covers typed answers."""
    answers = saved.get("answers") or {}
    batches, cur = [], []
    for q in qs:
        sug = q.get("suggest")
        prev = answers.get(q["id"])
        opts = []
        for v, label, desc in q["options"]:
            mark = []
            if v == sug or isinstance(sug, list) and v in sug:
                mark.append("suggested")
            if v == prev or isinstance(prev, list) and v in prev:
                mark.append("saved")
            opts.append({"label": label + (f" ({', '.join(mark)})" if mark else ""),
                         "description": _fmt(desc, alias, lock), "value": v})
        if len(opts) < 2:  # the tool needs two; the typed answer is the other path
            opts.append({"label": "Type it", "description": "choose the free-text option and type it", "value": None})
        cur.append({"id": q["id"], "header": q["header"][:12], "question": _fmt(q["text"], alias, lock),
                    "multiSelect": bool(q.get("multi")), "options": opts[:4]})
        if len(cur) == 4:
            batches.append(cur)
            cur = []
    if cur:
        batches.append(cur)
    return batches


def render_numbered(qs: List[dict], alias: str, lock: str, saved: dict) -> str:
    answers = saved.get("answers") or {}
    lines = [f"Setting up {alias}. Reply with one line per question, e.g. `1 b` or `3 a,c`; "
             "no reply leaves a question open (nothing is assumed)."]
    for n, q in enumerate(qs, 1):
        lines.append(f"{n}. {_fmt(q['text'], alias, lock)}" + (" (pick any)" if q.get("multi") else ""))
        for i, (v, label, desc) in enumerate(q["options"]):
            lines.append(f"   {chr(97 + i)}) {label}: {_fmt(desc, alias, lock)}")
        sug = q.get("suggest")
        extra = [f"suggested: {', '.join(sug) if isinstance(sug, list) else sug}"] if sug else []
        if q["id"] in answers:
            prev = answers[q["id"]]
            extra.append(f"saved: {', '.join(prev) if isinstance(prev, list) else prev}")
        if q.get("other"):
            extra.append("or type a value")
        if extra:
            lines.append(f"   ({'; '.join(extra)})")
    return "\n".join(lines)


def parse_value(q: dict, raw) -> object:
    """A validated answer for `q`; ValueError on a bad one."""
    values = [v for v, _, _ in q["options"]]
    labels = {label.lower(): v for v, label, _ in q["options"]}

    def one(s) -> str:
        s = re.sub(r"\s*\((suggested|saved)(, saved)?\)$", "", str(s).strip())  # a label as render_ask marks it
        return labels.get(s.lower(), s)
    if q.get("multi"):
        items = raw if isinstance(raw, list) else str(raw).split(",")
        items = [one(s) for s in items if str(s).strip()]
        bad = [s for s in items if s not in values and s != "none"]
        if bad or not items:
            raise ValueError(f"{q['id']}: pick from {', '.join(values)}, not {', '.join(bad) or 'nothing'}")
        if "none" in items:
            if len(items) > 1:
                raise ValueError(f"{q['id']}: none goes alone")
            return []
        return sorted(set(items), key=values.index)
    if raw is None or not str(raw).strip():
        raise ValueError(f"{q['id']}: an answer is needed, not an empty one")
    s = one(raw)
    if s in values:
        return s
    if q.get("other") and q["other"](s):
        return s
    raise ValueError(f"{q['id']}: {s!r} is not one of {', '.join(values)}"
                     + (" or a valid typed value" if q.get("other") else ""))


def merge(saved: dict, new: Dict[str, object]) -> dict:
    """`saved` with `new` answers recorded; a changed answer drops the answers that depend on it."""
    answers = dict(saved.get("answers") or {})
    asked = dict(saved.get("asked") or {})
    for qid, raw in new.items():
        q = BY_ID.get(qid)
        if q is None:
            raise ValueError(f"unknown question {qid!r} (known: {', '.join(BY_ID)})")
        val = parse_value(q, raw)
        if qid in answers and answers[qid] != val:
            for dep in _dependents(qid):
                if dep not in new:
                    answers.pop(dep, None)
                    asked.pop(dep, None)
        answers[qid] = val
        asked[qid] = qhash(q)
    return {"answers": answers, "asked": asked,
            "saved": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}


def _dependents(qid: str) -> List[str]:
    out = [q["id"] for q in QUESTIONS if q.get("parent") == qid]
    return out + [d for c in out for d in _dependents(c)]


def _sh(path: str) -> str:
    """`path` as a remote shell word: a plain path (also ~/...) as is, so ~ expands on the box."""
    return path if all(c.isalnum() or c in "/._-~" for c in path) else shlex.quote(path)


def lock_name(alias: str) -> str:
    """The device lock; `lock` only says whether your projects share its slots (`ttp machines add --shared`)."""
    return f"{alias}-device"


def plan(alias: str, entry: dict, answers: dict, plugin_root: str = "<plugin-root>") -> List[dict]:
    """Ordered steps, each {"id", "action", "check"}; every answer becomes an action and its check."""
    box_py = f"{plugin_root}/skills/setup-box/scripts/box.py"
    root = entry.get("workdir_root", "<workdir_root>")
    lock = lock_name(alias)
    steps: List[dict] = []

    def step(qid, action, check):
        steps.append({"id": qid, "action": action, "check": check})

    if answers.get("broker") == "install":
        ref = answers.get("broker.ref", "latest-release")
        step("broker", f"install tt-device-mcp ({ref}) on {alias} through the tenstorrent/tt-device-mcp "
                       "repository's documented installer; never hand-edit an installed broker",
             f"{box_py} bare check {alias}  (expect: broker: tt-device-mcp present, tt-device-broker active)")
    elif answers.get("broker") == "none":
        step("broker", f"no broker: every command that touches the device runs as "
                       f"`ttp lock {lock} -- <command>`, held only for that command",
             f"ttp lock --probe {lock}  (exit 0 free, 75 busy or paused)")
    checks = answers.get("host_checks") or []
    if checks:
        step("host_checks", "nothing to change: the checks are read-only; fix what they report with tt-buddy's "
                            "`recipes/developer-setup.md`",
             f"{box_py} setup hostcheck {alias} --only {','.join(checks)}")
    ws = answers.get("workspace")
    if ws and ws != "none":
        name = ws.split("/")[1]
        path = f"{root.rstrip('/')}/{name}"
        branch = answers.get("workspace.branch", "main")
        cc = " with ccache (`build_metal.sh --help` names its flag)" if answers.get("workspace.ccache") == "yes" else ""
        step("workspace", f"on {alias}: clone {ws} at {branch} into {path}, "
                          "`git submodule update --init --recursive`, and build in place" + cc +
                          " as tt-buddy's `recipes/workspace.md` and `recipes/tt-metal/build.md` say",
             f"ssh {alias} git -C {_sh(path)} rev-parse --abbrev-ref HEAD  (expect: {branch}, "
             "or a detached HEAD at a tag)")
    env = answers.get("python_env")
    if env == "pip-ttnn":
        venv = f"{root.rstrip('/')}/ttnn-venv"
        step("python_env", f"on {alias}: a virtualenv at {venv} with the ttnn wheel, as tt-buddy's "
                           "`recipes/developer-setup.md` says",
             f"ssh {alias} {_sh(venv)}/bin/python -m pip show ttnn")
    elif env == "workspace":
        if not ws or ws == "none":
            step("python_env", "WARNING: python_env is workspace but there is no workspace; ask python_env "
                               "again (pip-ttnn or none) or answer workspace first",
                 "none: no check until python_env or workspace is changed")
        else:
            where = f"{root.rstrip('/')}/{ws.split('/')[1]}"
            step("python_env", "the workspace's own env, built with it (tt-buddy's `recipes/tt-metal/env.md`); "
                               "a broker job needs it too (the `tt-buddy:run` skill's `skills/run/mcp.md`)",
                 f"ssh {alias} test -x {_sh(where)}/python_env/bin/python")
    for cli in answers.get("agent_clis") or []:
        step("agent_clis", f"install the {cli} CLI on {alias} for the user, per `agents.md`; the user logs in",
             f"ssh {alias} {AGENT_CLIS[cli]}")
    projects = answers.get("projects")
    if projects and projects != "none":
        who = "each of your projects" if projects == "all" else f"projects {projects}"
        step("projects", f"name {alias} in the Resources of {who}' charter (a charter edit, by the user or "
                         "its coordinator)", f"ttp machines status {alias}  (lists the projects using it)")
    owner = answers.get("recovery.owner")
    if owner:
        fb = answers.get("recovery.fallback", "none")
        o = "" if owner == "none" else owner
        f = "" if fb == "none" or not o else fb
        step("recovery", f"ttp machines add {alias} --owner {shlex.quote(o)} --fallback {shlex.quote(f)}",
             f"ttp machines status {alias}  (expect: "
             + (f"recovery owner {o}" + (f", fallback {f}" if f else "") if o else "no recovery owner") + ")")
    if answers.get("lock") == "shared":
        step("lock", f"ttp machines add {alias} --shared {lock}",
             f"ttp machines list --json  (expect: {alias} shared [{lock}])")
    elif answers.get("lock") == "per-project":
        step("lock", f"ttp machines add {alias} --unshared", f"ttp machines list --json  (expect: {alias} not shared)")
    if "disk_guard" in answers:
        step("disk_guard", f"ttp machines add {alias} --min-free-gb {answers['disk_guard']}",
             f"{box_py} bare check {alias}  (expect: guard {float(answers['disk_guard']):g} GB)")
    return steps


def save_site(path: pathlib.Path, site: dict) -> None:
    """Write the site file atomically, owner-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".boxes.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(site, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def hostcheck_script() -> str:
    """Read-only shell run on the box: sysfs, /proc and /dev listings only; it opens no device."""
    return (
        'echo "driver=$(cat /sys/module/tenstorrent/version 2>/dev/null)"; '
        'echo "dev_nodes=$(ls /dev/tenstorrent 2>/dev/null | wc -l)"; '
        'for f in /sys/class/tenstorrent/*/tt_fw_bundle_ver; do [ -r "$f" ] && '
        'echo "fw=$(basename "$(dirname "$f")") $(cat "$f")"; done; '
        'echo "hp1g=$(cat /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages 2>/dev/null)"; '
        'for d in /sys/bus/pci/devices/*; do '
        f'[ "$(cat "$d/vendor" 2>/dev/null)" = {TT_PCI_VENDOR} ] || continue; '
        'echo "pcie=$(basename "$d") $(cat "$d/current_link_speed" 2>/dev/null | tr " " _)'
        ' $(cat "$d/current_link_width" 2>/dev/null) $(cat "$d/max_link_speed" 2>/dev/null | tr " " _)'
        ' $(cat "$d/max_link_width" 2>/dev/null)"; done; true'
    )


def read_hostcheck(out: str, only=HOST_CHECKS) -> "tuple[List[str], List[str]]":
    """(report lines, problems) from the probe's key=value lines."""
    rows = [ln.split("=", 1) for ln in out.splitlines() if "=" in ln]
    kv = {k: v for k, v in rows}
    lines: List[str] = []
    problems: List[str] = []
    try:
        nodes = int(kv.get("dev_nodes", "0") or 0)
    except ValueError:
        nodes = 0
    if "driver" in only:
        ver = kv.get("driver", "").strip()
        if not ver:
            problems.append("driver")
            lines.append("driver: the tenstorrent module is not loaded: install it per tt-buddy's "
                         "`recipes/developer-setup.md`")
        elif not nodes:
            problems.append("driver")
            lines.append(f"driver: tenstorrent {ver} loaded but no /dev/tenstorrent nodes")
        else:
            lines.append(f"driver: tenstorrent {ver}, {nodes} device node(s)")
    if "firmware" in only:
        fw = [v for k, v in rows if k == "fw"]
        lines.append("firmware: " + ("; ".join(fw) if fw else
                     "not reported by the driver (older driver): check it per tt-buddy's "
                     "`recipes/developer-setup.md`"))
    if "hugepages" in only:
        hp = kv.get("hp1g", "").strip()
        lines.append(f"hugepages: {hp or 0} x 1 GB reserved" +
                     ("" if hp and hp != "0" else "; whether the cards need them: tt-buddy's "
                                                  "`recipes/developer-setup.md`"))
    if "pcie" in only:
        links = [v.split() for k, v in rows if k == "pcie"]
        if not links:
            problems.append("pcie")
            lines.append("pcie: no Tenstorrent card on the PCI bus")
        for ln in links:
            dev, cur, width, top, top_w = (ln + [""] * 5)[:5]
            low = cur != top or width != top_w
            lines.append(f"pcie: {dev} {cur.replace('_', ' ')} x{width}"
                         + (f", below its maximum {top.replace('_', ' ')} x{top_w}: check the slot and riser"
                            if low else ""))
    return lines, problems


def _entry(args) -> "tuple[dict, dict]":
    site = box.load_site(args.site)
    return site, box.box_entry(site, args.alias)


def cmd_questions(args) -> int:
    try:
        _, entry = _entry(args)
    except box.SiteError as e:
        print(f"error: {e}; write the box's entry first (site.md)")
        return 1
    saved = entry.get("setup") or {}
    qs = pending(saved, args.all)
    lock = lock_name(args.alias)
    if not qs:
        print(f"nothing to ask: every answer for {args.alias} is saved; `box.py setup plan {args.alias}` next")
        return 0
    if args.format == "numbered":
        print(render_numbered(qs, args.alias, lock, saved))
    else:
        print(json.dumps({"alias": args.alias, "batches": render_ask(qs, args.alias, lock, saved)}, indent=2))
    return 0


def cmd_save(args) -> int:
    new: Dict[str, object] = {}
    try:
        if args.json:
            doc = json.loads(args.json)
            if not isinstance(doc, dict):
                raise ValueError("--json must be an object of question id to answer")
            new.update(doc)
        for kv in args.set or []:
            if "=" not in kv:
                raise ValueError(f"--set {kv!r}: use id=value")
            k, v = kv.split("=", 1)
            new[k.strip()] = v
        site, entry = _entry(args)
        entry["setup"] = merge(entry.get("setup") or {}, new)
        save_site(box.site_path(args.site), site)
    except (ValueError, box.SiteError) as e:
        print(f"error: {e}")
        return 1
    left = pending(entry["setup"])
    print(f"saved {len(new)} answer(s) for {args.alias}; "
          + (f"still to ask: {', '.join(q['id'] for q in left)}" if left else "nothing left to ask"))
    return 0


def cmd_plan(args) -> int:
    try:
        _, entry = _entry(args)
    except box.SiteError as e:
        print(f"error: {e}")
        return 1
    saved = entry.get("setup") or {}
    left = pending(saved)
    if left:
        print(f"not yet: ask first ({', '.join(q['id'] for q in left)}); `box.py setup questions {args.alias}`")
        return 1
    steps = plan(args.alias, entry, saved.get("answers") or {}, args.plugin_root)
    if args.json:
        print(json.dumps(steps))
    else:
        for n, s in enumerate(steps, 1):
            print(f"{n}. [{s['id']}] {s['action']}\n   check: {s['check']}")
    return 0


def cmd_hostcheck(args) -> int:
    only = tuple(s for s in (args.only or ",".join(HOST_CHECKS)).split(",") if s)
    bad = [s for s in only if s not in HOST_CHECKS]
    if bad:
        print(f"error: unknown check {', '.join(bad)} (known: {', '.join(HOST_CHECKS)})")
        return 1
    r = box.ssh_run(args.alias, hostcheck_script(), timeout=60)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        print(f"ssh: connection to {args.alias} failed (exit {r.returncode}) {tail[0]}".rstrip())
        return 255 if r.returncode == 255 else 1
    lines, problems = read_hostcheck(r.stdout, only)
    if args.json:
        print(json.dumps({"alias": args.alias, "lines": lines, "problems": problems}))
    else:
        print("\n".join(lines + ["verdict: " + ("problems: " + ", ".join(problems) if problems else "ok")]))
    return 1 if problems else 0


def check_setup(alias: str, e: dict) -> List[str]:
    s = e.get("setup")
    if s is None:
        return []
    if not isinstance(s, dict) or not isinstance(s.get("answers", {}), dict) \
            or not isinstance(s.get("asked", {}), dict):
        return [f"{alias}: setup must be an object with answers and asked objects (`box.py setup save` writes it)"]
    return [f"{alias}: setup.answers has unknown question {k!r}" for k in s.get("answers", {}) if k not in BY_ID]


CHECKS = {route: check_setup for route in box.ROUTES}


def register(sub) -> None:
    st = sub.add_parser("setup", help="new-box flow: questions, saved answers, plan, host checks") \
        .add_subparsers(dest="setup_cmd", required=True)
    q = st.add_parser("questions", help="what is still to ask about a box (new, changed or unanswered)")
    q.add_argument("alias")
    q.add_argument("--format", choices=("ask", "numbered"), default="ask",
                   help="ask: batches for a multiple-choice question tool; numbered: a plain prompt")
    q.add_argument("--all", action="store_true", help="ask every question again")
    q.set_defaults(func=cmd_questions)
    s = st.add_parser("save", help="record answers in the box's site entry")
    s.add_argument("alias")
    s.add_argument("--set", action="append", metavar="ID=VALUE", help="one answer (lists comma-separated)")
    s.add_argument("--json", help="answers as one JSON object of question id to value")
    s.set_defaults(func=cmd_save)
    p = st.add_parser("plan", help="each saved answer as an action and its check")
    p.add_argument("alias")
    p.add_argument("--plugin-root", default="<plugin-root>", help="path the printed commands use")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)
    h = st.add_parser("hostcheck", help="read-only: driver, firmware, hugepages, PCIe link")
    h.add_argument("alias")
    h.add_argument("--only", help=f"comma-separated subset of {','.join(HOST_CHECKS)}")
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_hostcheck)
