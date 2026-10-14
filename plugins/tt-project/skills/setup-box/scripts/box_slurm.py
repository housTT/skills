"""setup-box Slurm route: `box.py slurm survey|check|submit|status|verify-clean` (see slurm.md).

Every Slurm command runs on the box's login node (`slurm.login` in the site file) over ssh.
"""

from __future__ import annotations

import datetime
import re
import shlex
import sys
from typing import List, Optional, Tuple

import box

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Slurm --time forms: M, M:S, H:M:S, D-H, D-H:M, D-H:M:S
TIME_RE = re.compile(r"^(\d+-\d+(:\d+){0,2}|\d+(:\d+){0,2})$")
FORBIDDEN = ("salloc", "--no-shell")


class Refused(Exception):
    pass


def expand_hostlist(expr: str) -> List[str]:
    """`n[01-03,07],m5` -> [n01, n02, n03, n07, m5] (one bracket group per name)."""
    out: List[str] = []
    for part in re.findall(r"[^,\[]+(?:\[[^\]]*\][^,\[]*)?", expr):
        m = re.match(r"^(.*?)\[([^\]]*)\](.*)$", part)
        if not m:
            out.append(part)
            continue
        pre, body, post = m.groups()
        for rng in body.split(","):
            lo, _, hi = rng.partition("-")
            if not hi:
                out.append(f"{pre}{lo}{post}")
                continue
            for i in range(int(lo), int(hi) + 1):
                out.append(f"{pre}{str(i).zfill(len(lo))}{post}")
    return out


def field(line: str, key: str) -> Optional[str]:
    m = re.search(rf"(?:^|\s){key}=(\S*)", line)
    return m.group(1) if m else None


def _name(kind: str, v: str) -> str:
    if not NAME_RE.match(v or ""):
        raise Refused(f"{kind} {v!r} is not a plain name")
    return v


def _box(args) -> Tuple[dict, str]:
    e = box.box_entry(box.load_site(args.site), args.box)
    sl = e.get("slurm")
    if e.get("route") != "slurm" or not isinstance(sl, dict) or not box._str(sl.get("login")):
        raise box.SiteError(f"{args.box} is not a slurm box with a slurm.login alias (site.md)")
    return e, sl["login"]


def _login(login: str, cmd: str, timeout: float = 120):
    r = box.ssh_run(login, cmd, timeout=timeout)
    if r.returncode == 255:
        raise box.SiteError(f"ssh {login} failed (exit 255): {r.stderr.strip()[-300:]}")
    return r


def check_node(e: dict, login: str, node: str, *, allow: Optional[List[str]] = None,
               min_idle_min: float = 0, partition: Optional[str] = None) -> str:
    """Raise Refused unless `node` may be taken now; return its scontrol line."""
    _name("node", node)
    if allow is not None and node not in allow:
        raise Refused(f"{node} is not in the allowed list ({', '.join(allow) or 'empty'})")
    for k in ("demo_nodes", "excluded_nodes"):
        if node in (e.get(k) or []):
            raise Refused(f"{node} is in the site file's {k}")
    r = _login(login, f"date +%Y-%m-%dT%H:%M:%S && scontrol -o show node {node}")
    lines = r.stdout.strip().splitlines()
    if r.returncode != 0 or len(lines) < 2:
        raise Refused(f"scontrol show node {node} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    now, line = lines[0], lines[1]
    state = field(line, "State") or "?"
    # Exactly IDLE: a suffix (IDLE*, IDLE~, IDLE$, ...) or +flag marks a node not to take.
    if state != "IDLE":
        raise Refused(f"{node} state is {state}, not plain IDLE")
    r = _login(login, "scontrol -o show reservation")
    if r.returncode != 0 and "No reservations" not in r.stdout + r.stderr:
        raise Refused(f"scontrol show reservation failed: {r.stderr.strip()[-300:]}")
    for res in r.stdout.splitlines():
        nodes = field(res, "Nodes") or ""
        if nodes == "ALL" or node in expand_hostlist(nodes):
            raise Refused(f"{node} is in reservation {field(res, 'ReservationName')}")
    if partition:
        _name("partition", partition)
        r = _login(login, f"sinfo -h -N -n {node} -p {partition} -o %N")
        if r.returncode != 0 or node not in r.stdout.split():
            raise Refused(f"{node} is not in partition {partition}")
    if min_idle_min > 0:
        last = field(line, "LastBusyTime") or "None"
        try:
            idle = (datetime.datetime.fromisoformat(now)
                    - datetime.datetime.fromisoformat(last)).total_seconds() / 60
        except ValueError:
            raise Refused(f"{node} LastBusyTime is {last}; cannot tell how long it has been idle") from None
        if idle < min_idle_min:
            raise Refused(f"{node} was busy {idle:.0f} min ago (LastBusyTime {last}); "
                          f"needs {min_idle_min:g} min idle")
    return line


def _rpath(p: str) -> str:
    """Quote a remote path for the shell, keeping a leading ~/ or $VAR expandable."""
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    m = re.match(r"^(\$\{?\w+\}?)(/.*)?$", p)
    if m:
        return f'"{m.group(1)}"' + (shlex.quote(m.group(2)) if m.group(2) else "")
    return shlex.quote(p)


def batch_script(checkout: str, command: str, rc: str, devices: Optional[str],
                 chips: Optional[str] = None, *, dev_dir: str = "/dev/tenstorrent",
                 tt_smi: str = "tt-smi") -> str:
    """The job: own process group, cd into the checkout, run, kill the group, write rc.

    With `chips` (a partition whose chip count is not measured yet) it first writes the number of
    devices in /dev/tenstorrent to that file and logs `tt-smi -ls` when the node has it.
    `dev_dir` and `tt_smi` exist for tests, so they never reach the host's own devices."""
    env = f"export TT_VISIBLE_DEVICES={shlex.quote(devices)}\n" if devices is not None else ""
    d, smi = shlex.quote(dev_dir), shlex.quote(tt_smi)
    measure = "" if chips is None else (
        f"n=$(ls {d} 2>/dev/null | grep -c '^[0-9][0-9]*$')\n"
        f"echo \"$n\" > {_rpath(chips)}\n"
        f'echo "chips: $n devices in "{d}\n'
        f"command -v {smi} >/dev/null && timeout 120 {smi} -ls\n")
    return f"""#!/bin/bash
# written by setup-box box.py slurm submit
if [ -z "$TTP_JOB_PGRP" ] && [ "$(ps -o pgid= -p $$ | tr -d ' ')" != "$$" ]; then
  export TTP_JOB_PGRP=1
  exec setsid /bin/bash "$0" "$@"
fi
rc_file={_rpath(rc)}
cleanup() {{
  rc=$?
  trap '' TERM INT HUP
  trap - EXIT
  kill -TERM -- -$$ 2>/dev/null
  for _ in 1 2 3 4 5; do
    [ -z "$(ps -eo pid=,pgid= | awk -v g=$$ -v me=$$ '$2 == g && $1 != me')" ] && break
    sleep 1
  done
  for p in $(ps -eo pid=,pgid= | awk -v g=$$ -v me=$$ '$2 == g && $1 != me {{print $1}}'); do
    kill -KILL "$p" 2>/dev/null
  done
  echo "$rc" > "$rc_file.tmp" && mv -f "$rc_file.tmp" "$rc_file"
  exit "$rc"
}}
trap cleanup EXIT
trap 'exit 143' TERM INT HUP
rm -f "$rc_file"
{measure}cd {_rpath(checkout)} || exit 97
{env}echo "job ${{SLURM_JOB_ID:-?}} on $(hostname) in $PWD at $(date -Is)"
/bin/bash -c {shlex.quote(command)} &
wait $!
"""


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return ".." not in path.split("/") and (path == root or path.startswith(root + "/"))


SHARED_BUSY = 3  # this many nodes with one LastBusyTime, to the second, point to a controller event


def _since(now: str, then: Optional[str]) -> Optional[float]:
    """Minutes from `then` to `now` (scontrol times), or None when `then` is not a time."""
    try:
        return (datetime.datetime.fromisoformat(now)
                - datetime.datetime.fromisoformat(then or "")).total_seconds() / 60
    except ValueError:
        return None


def survey(e: dict, login: str, partitions: List[str], min_idle_min: float = 0) -> List[str]:
    """Read-only: per partition, node states, plain-IDLE nodes with LastBusyTime, reservations.

    Runs only date, sinfo and scontrol on the login node; never sbatch, srun or salloc."""
    for p in partitions:
        _name("partition", p)
    r = _login(login, "date +%Y-%m-%dT%H:%M:%S; echo ---; "
                      f"sinfo -h -N -o '%N|%P|%T' -p {','.join(partitions)}; echo ---; "
                      "scontrol -o show node; echo ---; scontrol -o show reservation")
    now, nodes_out, info, res_out = (r.stdout.split("---\n") + ["", "", "", ""])[:4]
    now = now.strip()
    if not now or not nodes_out.strip():
        raise box.SiteError(f"sinfo found no nodes (exit {r.returncode}): {r.stderr.strip()[-300:]}")
    members = {p: {} for p in partitions}
    for ln in nodes_out.splitlines():
        node, part, state = (ln.strip().split("|") + ["", ""])[:3]
        part = part.rstrip("*")  # sinfo marks the default partition
        if part in members:
            members[part][node] = state
    show = {}
    for ln in info.splitlines():
        name = field(ln, "NodeName")
        if name:
            show[name] = ln
    busy = {}
    for ln in show.values():
        if _since(now, field(ln, "LastBusyTime")) is not None:
            busy.setdefault(field(ln, "LastBusyTime"), []).append(field(ln, "NodeName"))
    reservations = []
    for ln in res_out.splitlines():
        if field(ln, "ReservationName"):
            nodes = field(ln, "Nodes") or ""
            reservations.append((ln, "ALL" if nodes == "ALL" else set(expand_hostlist(nodes))))
    parts = {p.get("name"): p for p in (e.get("slurm") or {}).get("partitions", []) if isinstance(p, dict)}
    out = [f"survey at {now} (login node clock); read-only: sinfo and scontrol only"]
    for p in partitions:
        site = parts.get(p, {})
        size = (f"{site['chips']} chips" if box.chips_known(site)
                else "chips not measured yet" if site else "not in the site file")
        nodes = members[p]
        counts = {}
        for st in nodes.values():
            counts[st] = counts.get(st, 0) + 1
        out.append(f"partition {p} ({size}{', ' + site['board'] if site.get('board') else ''}): "
                   f"{len(nodes)} nodes" + (": " + ", ".join(f"{st} {n}" for st, n in sorted(counts.items()))
                                            if counts else ""))
        for node in sorted(nodes):
            ln = show.get(node, "")
            if field(ln, "State") != "IDLE":
                continue
            last = field(ln, "LastBusyTime") or "None"
            idle = _since(now, last)
            why = [f"reservation {field(res, 'ReservationName')}" for res, ns in reservations
                   if ns == "ALL" or node in ns]
            why += [k for k in ("demo_nodes", "excluded_nodes") if node in (e.get(k) or [])]
            if min_idle_min > 0 and (idle is None or idle < min_idle_min):
                why.append(f"under --min-idle {min_idle_min:g}")
            shared = len(busy.get(last, [])) if idle is not None else 0
            out.append(f"  idle {node} LastBusyTime {last}"
                       + (f" ({idle:.0f} min ago)" if idle is not None else " (unknown)")
                       + (f" shared by {shared} nodes" if shared >= SHARED_BUSY else "")
                       + (f": not takeable ({'; '.join(why)})" if why else ""))
        for res, ns in reservations:
            if ns == "ALL" or ns & set(nodes):
                out.append(f"  reservation {field(res, 'ReservationName')} nodes {field(res, 'Nodes')} "
                           f"{field(res, 'StartTime')} to {field(res, 'EndTime')} {field(res, 'State') or ''}".rstrip())
    for last, names in sorted(busy.items()):
        if len(names) >= SHARED_BUSY:
            out.append(f"note: {len(names)} nodes share LastBusyTime {last}: likely a controller restart "
                       f"or reconfigure, not jobs. A --min-idle check is then conservative; "
                       f"`sacct -N <node>` shows when the last real job ended (where accounting is on)")
    return out


def cmd_survey(args) -> int:
    try:
        e, login = _box(args)
        partitions = args.partition or [p.get("name") for p in e["slurm"].get("partitions", [])
                                        if isinstance(p, dict) and p.get("name")]
        if not partitions:
            raise box.SiteError(f"{args.box} lists no partitions; pass --partition")
        lines = survey(e, login, partitions, args.min_idle)
    except (Refused, box.SiteError) as err:
        print(f"error: {err}")
        return 1
    print("\n".join(lines))
    return 0


def cmd_check(args) -> int:
    try:
        e, login = _box(args)
        check_node(e, login, args.node, allow=args.allow, min_idle_min=args.min_idle,
                   partition=args.partition)
    except (Refused, box.SiteError) as err:
        print(f"refused: {err}")
        return 1
    print(f"ok {args.node} is IDLE and in no reservation")
    return 0


def cmd_submit(args) -> int:
    command = args.command
    try:
        if not args.time:
            raise Refused("--time is required: the expected run time plus a small margin")
        if not TIME_RE.match(args.time):
            raise Refused(f"--time {args.time!r} is not a Slurm time limit (e.g. 90, 1:30:00, 1-0)")
        try:
            words = shlex.split(command, comments=True)
        except ValueError as err:
            raise Refused(f"--command cannot be parsed ({err})") from None
        if any(f in words for f in FORBIDDEN):
            raise Refused("salloc and --no-shell are not allowed: submit one self-ending batch job")
        _name("job name", args.name)
        e, login = _box(args)
        part = next((p for p in e["slurm"].get("partitions", []) if p.get("name") == args.partition), None)
        if part is None:
            raise Refused(f"partition {args.partition!r} is not in the site file for {args.box}")
        root = e.get("workdir_root", "")
        rundir = args.rundir or f"{root.rstrip('/')}/runs/{args.name}"
        for what, p in (("checkout", args.checkout), ("run dir", rundir)):
            if not _under(p, root):
                raise Refused(f"{what} {p} is not under workdir_root {root} (shared storage)")
        check_node(e, login, args.node, allow=args.allow, min_idle_min=args.min_idle,
                   partition=args.partition)
    except (Refused, box.SiteError) as err:
        print(f"refused: {err}")
        return 1
    log, rc, script = f"{rundir}/job.log", f"{rundir}/job.rc", f"{rundir}/job.sbatch"
    chips = None if box.chips_known(part) else f"{rundir}/job.chips"
    sbatch = ["sbatch", "--parsable", "--exclusive", "-w", args.node, "-p", args.partition,
              f"--time={args.time}", f"--job-name={args.name}"]
    # A job reusing --name must not leave an earlier job's count for status to print.
    remote = (f"mkdir -p {_rpath(rundir)} && rm -f {_rpath(rundir)}/job.chips && "
              f"cat > {_rpath(script)}.tmp && "
              f"mv -f {_rpath(script)}.tmp {_rpath(script)} && "
              f"{shlex.join(sbatch)} --output={_rpath(log)} --error={_rpath(log)} {_rpath(script)}")
    r = box.ssh_run(login, remote, input=batch_script(args.checkout, command, rc, args.devices, chips))
    job = r.stdout.strip().split(";")[0].splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode != 0 or not job.isdigit():
        print(f"error: sbatch failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-500:]}")
        return 1
    start = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    size = f"{part['chips']} chips" if chips is None else "chips not measured yet: the job records them"
    print(f"job {job} node {args.node} partition {args.partition} ({size}, "
          f"{part.get('board', '?')}) log {log} rc {rc}")
    print("note: ttp note " + shlex.quote(
        f"slurm alloc: cluster {args.box} job {job} node {args.node} start {start} "
        f"limit {args.time} purpose: {args.purpose}"))
    probe = (f"ids=$(squeue -h -o %i -u \"$USER\") || exit 1; "
             f"printf '%s\\n' \"$ids\" | grep -qx {job} && exit 1; exit 0")
    print("retry_when: " + shlex.join(["ssh", *box.SSH_OPTS, login, probe]))
    return 0


def cmd_status(args) -> int:
    try:
        _name("job id", args.job)
        e, login = _box(args)
        r = _login(login, f"squeue -h -j {args.job} -o '%i|%T|%N|%M|%l|%R'")
    except (Refused, box.SiteError) as err:
        print(f"error: {err}")
        return 1
    # A failed squeue (controller down, timeout 124) says nothing about the job: never report it
    # ended, or the caller may resubmit a job that still runs. Only "Invalid job id" means gone.
    if r.returncode != 0 and "invalid job id" not in (r.stderr + r.stdout).lower():
        print(f"error: squeue failed (exit {r.returncode}), job state unknown: "
              f"{(r.stderr or r.stdout).strip()[-300:]}")
        return 2
    row = next((ln for ln in r.stdout.splitlines() if ln.split("|")[0] == args.job), None)
    if row:
        _, st, node, used, limit, reason = (row.split("|") + [""] * 6)[:6]
        print(f"job {args.job} {st} node {node or '-'} elapsed {used} limit {limit} {reason}".rstrip())
        return 0
    print(f"job {args.job} is no longer in squeue (ended)")
    rundir = args.rundir or (f"{e.get('workdir_root', '').rstrip('/')}/runs/{args.name}" if args.name else None)
    if rundir:
        r = _login(login, f"cat {_rpath(rundir)}/job.rc 2>/dev/null || echo missing; "
                          f"echo ---; cat {_rpath(rundir)}/job.chips 2>/dev/null; "
                          f"echo ---; tail -n 20 {_rpath(rundir)}/job.log 2>/dev/null")
        rc, _, rest = r.stdout.partition("---\n")
        chips, _, tail = rest.partition("---\n")
        print(f"rc {rc.strip()} (missing: SIGKILL before its trap ran, node failure, or the job "
              f"never started)" if rc.strip() == "missing" else f"rc {rc.strip()}")
        if chips.strip():
            print(f"measured chips: {chips.strip()} devices in /dev/tenstorrent on the job's node; "
                  f"record the partition's chips in the site file (site.md). A card with two chips "
                  f"behind one device counts once: check the tt-smi -ls output in job.log")
        print(tail.rstrip())
    return 0


def cmd_verify_clean(args) -> int:
    try:
        _name("node", args.node)
        e, login = _box(args)
        r = _login(login, f"squeue -h -u \"$USER\" -w {args.node} -o '%i %T'")
    except (Refused, box.SiteError) as err:
        print(f"error: {err}")
        return 2
    if r.returncode != 0:
        print(f"error: squeue failed: {r.stderr.strip()[-300:]}")
        return 2
    if r.stdout.strip():
        print(f"not clean: jobs of yours on {args.node}: {' '.join(r.stdout.split())}")
        return 1
    print(f"clean: no job of yours on {args.node} in squeue")
    if not args.ssh:
        print(f"cannot check processes on {args.node}: ssh to a compute node is allowed only during "
              f"your own allocation (pass --ssh where it is always allowed)")
        return 0
    r = _login(login, f"ssh -o BatchMode=yes -o ConnectTimeout=15 {args.node} 'ps -u \"$USER\" -o pid=,comm='")
    if r.returncode != 0:
        print(f"cannot check processes on {args.node}: ssh failed (exit {r.returncode})")
        return 0
    procs = [ln for ln in r.stdout.splitlines() if ln.split()[1:2] not in (["ps"], ["sshd"])]
    if procs:
        print(f"not clean: processes of yours on {args.node}: {'; '.join(p.strip() for p in procs)}")
        return 1
    print(f"clean: no process of yours on {args.node}")
    return 0


def _csv(v: str) -> List[str]:
    return [x for x in (s.strip() for s in v.split(",")) if x]


def _common(p, node=True):
    p.add_argument("box", help="the box's site-file alias (route slurm)")
    if node:
        p.add_argument("--allow", type=_csv, default=None,
                       help="comma list of nodes the project's charter allows (default: no list)")
        p.add_argument("--min-idle", type=float, default=0, metavar="MIN",
                       help="minutes since LastBusyTime the charter asks for (default 0: no minimum)")


def register(sub) -> None:
    sl = sub.add_parser("slurm", help="Slurm route (slurm.md)").add_subparsers(dest="slurm_cmd", required=True)
    p = sl.add_parser("survey", help="read-only: node states, idle nodes and reservations per partition")
    _common(p, node=False)
    p.add_argument("--partition", action="append", help="survey this partition (repeatable; default: "
                                                         "the site file's partitions)")
    p.add_argument("--min-idle", type=float, default=0, metavar="MIN",
                   help="mark idle nodes last busy under MIN minutes ago (default 0: no minimum)")
    p.set_defaults(func=cmd_survey)
    p = sl.add_parser("check", help="may this node be taken now?")
    _common(p)
    p.add_argument("node")
    p.add_argument("--partition")
    p.set_defaults(func=cmd_check)
    p = sl.add_parser("submit", help="submit one self-ending batch job on an exclusive node")
    _common(p)
    p.add_argument("--node", required=True)
    p.add_argument("--partition", required=True)
    p.add_argument("--time", help="expected run time plus a small margin (required)")
    p.add_argument("--name", required=True, help="job name; also the run dir under workdir_root/runs")
    p.add_argument("--checkout", required=True, help="the checkout on shared storage to cd into")
    p.add_argument("--purpose", required=True, help="one line for the allocation note")
    p.add_argument("--rundir", help="where job.sbatch, job.log and job.rc go (default workdir_root/runs/<name>)")
    p.add_argument("--devices", help="TT_VISIBLE_DEVICES for the job, e.g. 0 for one chip")
    p.add_argument("--command", required=True, help="the shell command to run in the checkout")
    p.set_defaults(func=cmd_submit)
    p = sl.add_parser("status", help="a job's state, or its rc and log tail once it ended")
    _common(p, node=False)
    p.add_argument("job")
    p.add_argument("--name", help="the job name given to submit (finds its run dir)")
    p.add_argument("--rundir")
    p.set_defaults(func=cmd_status)
    p = sl.add_parser("verify-clean", help="confirm nothing of yours remains on a node")
    _common(p, node=False)
    p.add_argument("node")
    p.add_argument("--ssh", action="store_true", help="also list your processes on the node over ssh")
    p.set_defaults(func=cmd_verify_clean)


if __name__ == "__main__":
    sys.exit("run this through box.py slurm ...")
