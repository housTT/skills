#!/usr/bin/env bash
# setup-box IRD route, end to end (ird.md): run it where the IRD management host is reachable.
#
#   ird_e2e.sh [--box <alias>] [--mgmt <alias>] [--pool <pool>] [--arch <arch>] [--workdir <path>]
#              [--repo <git-url>] [--timeout hh:mm:ss] [--extend-min N] [--site <file>] [--dry-run]
#
# Values come from the box's entry in the site file (box.py site, `ird.arch` for the arch), and
# flags override them. Steps: 1 pick a free, non-draining machine; 2 reserve it; 3 ssh in through
# the managed ssh include (ProxyJump); 4 clone a small public repo into a per-run worktree, edit
# it, build a trivial target, run `tt-smi -s`; 5 extend once; 6 release and check `ird list`.
# On any failure or Ctrl-C the trap releases the reservation this run made, and only that one.
# --dry-run runs step 1 only and prints the rest. Ends with a short report to paste back.
# Needs bash 3.2+, python3 and ssh. Exit: 0 pass, 1 a step failed, 2 bad usage, 130 interrupted.

set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
BOX_PY="$HERE/box.py"
SITE_IN=${TTP_BOXES_FILE:-$HOME/.tt-project/boxes.json}
IRD_DIR=${TTP_IRD_DIR:-$HOME/.tt-project/ird}
BOX="" MGMT="" POOL="" ARCH="" WORKDIR="" DRY=0
REPO="https://github.com/octocat/Hello-World.git"
TIMEOUT="00:30:00" EXTEND_MIN=15

usage() { sed -n '4,5p' "$0" | sed 's/^# *//'; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --box) BOX=${2:-}; shift 2 ;;
    --mgmt) MGMT=${2:-}; shift 2 ;;
    --pool) POOL=${2:-}; shift 2 ;;
    --arch) ARCH=${2:-}; shift 2 ;;
    --workdir) WORKDIR=${2:-}; shift 2 ;;
    --repo) REPO=${2:-}; shift 2 ;;
    --timeout) TIMEOUT=${2:-}; shift 2 ;;
    --extend-min) EXTEND_MIN=${2:-}; shift 2 ;;
    --site) SITE_IN=${2:-}; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

RUN_ID="$(date +%Y%m%d%H%M%S)-$$"
RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ird-e2e.XXXXXX") || { echo "cannot make a temp dir" >&2; exit 1; }
SITE="$RUN_DIR/boxes.json"
REPORT="$RUN_DIR/report.txt"
# box.py claims: this run is one project task, so release passes its ownership gate.
export TTP_PROJECT="ird-e2e" TTP_TASK="ird-e2e-$RUN_ID"
unset TTP_RUN_DIR

RID="" MACHINE="" REMOTE_ROOT="" RESERVING=0 STEP="setup" RESULT="" RELEASED="n/a"
NOTES=()

say() { printf '%s\n' "$*"; }
note() { NOTES+=("$*"); say "$*"; }
fail() { RESULT="FAIL at $STEP: $*"; exit 1; }
box() { python3 "$BOX_PY" --site "$SITE" "$@"; }
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
on_machine() { ssh -n -o BatchMode=yes -o ConnectTimeout=20 "$MACHINE" "$1"; }
ird_rows() { ssh -n -o BatchMode=yes -o ConnectTimeout=20 "$MGMT" 'ird list --noheader'; }
row_of() { ird_rows | awk -v id="$1" '$1 == id' ; }

report() {
  {
    say "--- ird-e2e report (paste this back) ---"
    say "when: $(date '+%Y-%m-%d %H:%M %Z')  host: $(uname -s) $(uname -r)  $(ssh -V 2>&1)"
    say "plugin: $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
      "$HERE/../../../.claude-plugin/plugin.json" 2>/dev/null || echo '?')  mode: $([ "$DRY" = 1 ] && echo dry-run || echo live)"
    say "pool: ${POOL:-?}  arch: ${ARCH:-?}  timeout: $TIMEOUT  repo: $REPO"
    for l in "${NOTES[@]+"${NOTES[@]}"}"; do say "$l"; done
    say "reservation: ${RID:-none}  released: $RELEASED"
    say "result: ${RESULT:-PASS}"
    say "--- end ---"
  } > "$REPORT"
  say ""
  cat "$REPORT"
  say "(saved to $REPORT)"
}

on_exit() {
  local rc=$?
  trap '' INT TERM HUP
  [ $rc -ne 0 ] && [ -z "$RESULT" ] && RESULT="FAIL at $STEP (exit $rc)"
  if [ -n "$REMOTE_ROOT" ]; then
    on_machine "rm -rf -- $(q "$REMOTE_ROOT")" >/dev/null 2>&1 || note "cleanup: could not remove $REMOTE_ROOT"
  fi
  if [ -n "$RID" ] && [ "$RELEASED" != "yes" ]; then
    say "releasing reservation $RID (made by this run) ..."
    if box ird release "$BOX" "$RID"; then RELEASED="yes (by the trap)"
    else RELEASED="NO: release it by hand: python3 $BOX_PY ird release $BOX $RID"; fi
  elif [ "$RESERVING" = 1 ] && [ -z "$RID" ]; then
    RELEASED="unknown: reserve did not finish; check python3 $BOX_PY ird status $BOX"
  fi
  report
  exit $rc
}
trap on_exit EXIT
trap 'RESULT="interrupted at $STEP"; exit 130' INT TERM HUP

# --- values: the site entry, then flags; one temp site file for box.py ---------------------------
STEP="values"
command -v python3 >/dev/null || fail "python3 not found"
command -v ssh >/dev/null || fail "ssh not found"
# The helper goes to a file first: bash 3.2 (macOS) misparses a heredoc inside $(...).
cat > "$RUN_DIR/values.py" <<'PY'
import json, os, sys
src, alias, mgmt, pool, arch, workdir, out = sys.argv[1:]
try:
    boxes = json.load(open(os.path.expanduser(src))).get("boxes") or {}
except FileNotFoundError:
    boxes = {}
except ValueError as e:
    sys.exit(f"site file {src} is not JSON: {e}")
if not alias:
    ird = [k for k, v in boxes.items() if isinstance(v, dict) and v.get("route") == "ird"]
    if len(ird) == 1:
        alias = ird[0]
    elif len(ird) > 1:
        sys.exit(f"the site file has {len(ird)} ird boxes: pick one with --box")
if alias and alias not in boxes and not mgmt:
    sys.exit(f"no box {alias!r} in {src}")
entry = dict(boxes.get(alias) or {}) if alias else {}
cfg = dict(entry.get("ird") or {})
pools = list(cfg.get("pools") or [])
mgmt = mgmt or cfg.get("mgmt", "")
pool = pool or (pools[0] if pools else "")
arch = arch or cfg.get("arch", "")
workdir = workdir or entry.get("workdir_root", "")
missing = [n for n, v in (("--mgmt", mgmt), ("--pool", pool), ("--arch", arch), ("--workdir", workdir)) if not v]
if missing:
    sys.exit("missing " + ", ".join(missing) + f": set them in the site file ({src}, see site.md) or pass them")
if not workdir.startswith("/"):
    sys.exit("--workdir must be an absolute path on the machine")
alias = alias or "ird-e2e"
cfg.update(mgmt=mgmt, pools=[pool] + [p for p in pools if p != pool])
entry.update(route="ird", workdir_root=workdir, ird=cfg)
json.dump({"version": 1, "boxes": {alias: entry}}, open(out, "w"), indent=2)
for k, v in (("BOX", alias), ("MGMT", mgmt), ("POOL", pool), ("ARCH", arch), ("WORKDIR", workdir)):
    print(f"{k}\t{v}")
PY
VALS=$(python3 "$RUN_DIR/values.py" "$SITE_IN" "$BOX" "$MGMT" "$POOL" "$ARCH" "$WORKDIR" "$SITE" 2>&1) \
  || fail "$VALS"
while IFS=$'\t' read -r k v; do
  case "$k" in BOX) BOX=$v ;; MGMT) MGMT=$v ;; POOL) POOL=$v ;; ARCH) ARCH=$v ;; WORKDIR) WORKDIR=$v ;; esac
done <<< "$VALS"
[[ "$TIMEOUT" =~ ^[0-9]{1,3}:[0-5][0-9]:[0-5][0-9]$ ]] || fail "--timeout must be hh:mm:ss"
[[ "$EXTEND_MIN" =~ ^[0-9]+$ ]] || fail "--extend-min must be whole minutes"
INCLUDE="$IRD_DIR/ssh-hosts"
if ! grep -qsF -e "tt-project/ird/ssh-hosts" -e "$INCLUDE" "$HOME/.ssh/config"; then
  [ "$DRY" = 1 ] || fail "$HOME/.ssh/config lacks 'Include $INCLUDE' at its top (ird.md); add it and rerun"
  note "note: ~/.ssh/config lacks 'Include $INCLUDE' at its top; a live run stops here"
fi

# --- 1 list and pick ------------------------------------------------------------------------------
STEP="1 list"; T0=$(date +%s)
LIST=$(box ird list "$BOX") || fail "box.py ird list: $(printf '%s' "$LIST" | tail -n 3)"
FREE=$(printf '%s\n' "$LIST" | awk '$1 == "free" {print $2}')
MACHINE=$(printf '%s\n' "$FREE" | sed -n 1p)
[ -n "$MACHINE" ] || fail "no free, non-draining machine in pool $POOL"
note "step 1 list: PASS $(printf '%s\n' "$FREE" | grep -c .) free, picked $MACHINE ($(( $(date +%s) - T0 ))s)"

RESERVE=(ird reserve "$BOX" --arch "$ARCH" --timeout "$TIMEOUT" --pool "$POOL" --machine "$MACHINE" --wait 120)
if [ "$DRY" = 1 ]; then
  note "step 2 reserve: DRY box.py ${RESERVE[*]}"
  note "step 3 ssh: DRY ssh $MACHINE (ProxyJump $MGMT via $INCLUDE)"
  note "step 4 work: DRY clone $REPO into $WORKDIR/ird-e2e/$RUN_ID, worktree, edit, build, tt-smi -s"
  note "step 5 extend: DRY box.py ird extend $BOX <id> $EXTEND_MIN"
  note "step 6 release: DRY box.py ird release $BOX <id>, then check ird list"
  RESULT="PASS (dry-run: steps 2-6 not run)"
  exit 0
fi

# --- 2 reserve ------------------------------------------------------------------------------------
STEP="2 reserve"; T0=$(date +%s)
RESERVING=1
OUT=$(box "${RESERVE[@]}"); rc=$?
RID=$(printf '%s\n' "$OUT" | awk '$1 == "reserved" {print $2; exit}')
case "$OUT" in *"made no reservation"*) RESERVING=0 ;; esac
[ $rc -eq 0 ] && [ -n "$RID" ] || fail "box.py ird reserve (rc $rc): $(printf '%s' "$OUT" | tail -n 3)"
EXPIRES=$(printf '%s\n' "$OUT" | sed -n 's/^reserved .* expires=\([^ ]*\).*/\1/p')
MACHINE=$(printf '%s\n' "$OUT" | sed -n 's/^reserved .* machine=\([^ ]*\).*/\1/p')
ROW1=$(row_of "$RID")
note "step 2 reserve: PASS id $RID on $MACHINE, expires $EXPIRES ($(( $(date +%s) - T0 ))s)"
note "  ird list row: ${ROW1:-<none>}"

# --- 3 ssh in -------------------------------------------------------------------------------------
STEP="3 ssh"; T0=$(date +%s)
grep -qs "^# >>> tt-project ird $RID\$" "$INCLUDE" || fail "no ssh block for $RID in $INCLUDE"
JUMP=$(ssh -G "$MACHINE" 2>/dev/null | awk '$1 == "proxyjump" {print $2}')
[ "$JUMP" = "$MGMT" ] || fail "ssh -G $MACHINE gives proxyjump '${JUMP:-none}', not $MGMT: is the Include at the top of ~/.ssh/config?"
if [ -t 0 ]; then
  say "first ssh to $MACHINE: if asked, check its host key (ssh-workflow.md) before you accept it"
  ssh -o ConnectTimeout=20 "$MACHINE" true || fail "ssh $MACHINE failed"
fi
HOSTNAME_R=$(on_machine 'hostname') || fail "ssh -o BatchMode=yes $MACHINE failed (host key not yet in known_hosts?)"
note "step 3 ssh: PASS via ProxyJump, remote hostname $HOSTNAME_R ($(( $(date +%s) - T0 ))s)"

# --- 4 clone, worktree, edit, build, tt-smi -------------------------------------------------------
STEP="4 work"; T0=$(date +%s)
REMOTE_ROOT="$WORKDIR/ird-e2e/$RUN_ID"
SCRIPT="set -e; r=$(q "$REMOTE_ROOT"); mkdir -p \"\$r\"; cd \"\$r\"
git clone -q --depth 1 $(q "$REPO") src
cd src; git worktree add -q -b ird-e2e-$RUN_ID ../wt; cd ../wt
printf '#include <stdio.h>\nint main(void) { puts(\"ird-e2e build ok\"); return 0; }\n' > ird_e2e_hello.c
n=\$(git status --short | wc -l); echo \"edited \$((n)) file(s)\"
cc -o ird_e2e_hello ird_e2e_hello.c; ./ird_e2e_hello"
WORK=$(on_machine "$SCRIPT" 2>&1) || fail "clone/edit/build: $(printf '%s' "$WORK" | tail -n 3)"
SMI=$(on_machine "tt-smi -s" 2>&1); rc=$?
[ $rc -eq 0 ] || fail "tt-smi -s (rc $rc): $(printf '%s' "$SMI" | tail -n 3)"
BOARDS=$(printf '%s\n' "$SMI" | grep -o '"board_type": *"[^"]*"' | sed 's/.*: *"\([^"]*\)"/\1/' | sort | uniq -c | sed 's/^ *\([0-9]*\) /\1x /' | paste -s -d, -)
note "step 4 work: PASS $(printf '%s' "$WORK" | tr '\n' ';' | sed 's/;$//'); tt-smi -s ok, boards: ${BOARDS:-none listed} ($(( $(date +%s) - T0 ))s)"

# --- 5 extend once --------------------------------------------------------------------------------
STEP="5 extend"; T0=$(date +%s)
OUT=$(box ird extend "$BOX" "$RID" "$EXTEND_MIN") || fail "box.py ird extend: $(printf '%s' "$OUT" | tail -n 3)"
NEW=$(printf '%s\n' "$OUT" | sed -n 's/.*expires~\([^ ]*\).*/\1/p')
[ -n "$NEW" ] || fail "box.py ird extend printed no expiry: $OUT"
STATUS=$(box ird status "$BOX") || fail "box.py ird status: $STATUS"
printf '%s\n' "$STATUS" | grep -q "^$RID .*expires=$NEW" || fail "status does not show $RID expiring $NEW: $STATUS"
ROW2=$(row_of "$RID")
[ -n "$ROW2" ] || fail "reservation $RID is gone from ird list after the extend"
if [ "$ROW2" != "$ROW1" ]; then SEEN="ird list row changed"; else SEEN="ird list row unchanged"; fi
note "step 5 extend: PASS +$EXTEND_MIN min, expires $EXPIRES -> $NEW; $SEEN ($(( $(date +%s) - T0 ))s)"
note "  ird list row: $ROW2"

# --- 6 release ------------------------------------------------------------------------------------
STEP="6 release"; T0=$(date +%s)
on_machine "rm -rf -- $(q "$REMOTE_ROOT")" >/dev/null 2>&1 && REMOTE_ROOT=""
OUT=$(box ird release "$BOX" "$RID") || fail "box.py ird release: $(printf '%s' "$OUT" | tail -n 3)"
RELEASED="yes"
ROWS=$(ird_rows) || fail "ird list after release failed"
printf '%s\n' "$ROWS" | awk -v id="$RID" '$1 == id {f=1} END {exit !f}' && fail "$RID still in ird list after release"
grep -qs "^# >>> tt-project ird $RID\$" "$INCLUDE" && fail "ssh block for $RID still in $INCLUDE"
note "step 6 release: PASS $RID gone from ird list, ssh block removed ($(( $(date +%s) - T0 ))s)"
