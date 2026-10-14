"""Packaging invariants for the tt-project plugin."""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import sys

import pytest
import yaml

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-project"
SKILLS = PLUGIN / "skills"
RUNTIME = PLUGIN / "runtime" / "ttp"
MANIFESTS = [PLUGIN / d / "plugin.json" for d in (".claude-plugin", ".codex-plugin", ".cursor-plugin")]

# Generic secret shapes and personal paths that must never ship in the open-source plugin.
FORBIDDEN = [
    r"xox[abprs]-[0-9A-Za-z]", r"sk-ant-", r"\bsk-[A-Za-z0-9]{32,}", r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"github_pat_", r"\bglpat-[0-9A-Za-z_-]{20}", r"\bAKIA[0-9A-Z]{16}\b", r"\bAIza[0-9A-Za-z_-]{35}\b",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"/home/[a-z]+/", r"/Users/[A-Za-z0-9._-]+/",
]

# Setup-specific patterns (internal domains, host names, user names, project names) never live in
# this repository. Each maintainer keeps them in a local file outside it: $TTP_LEAK_PATTERNS_FILE,
# else $XDG_CONFIG_HOME/tt-project/leak-patterns.txt (default ~/.config/tt-project/leak-patterns.txt).
# One regex per line, compiled on its own (so a leading (?i) works); blank lines and lines starting
# with # are ignored. Without the file that check is skipped with a visible reason; a malformed
# pattern fails it.
LEAK_PATTERNS_ENV = "TTP_LEAK_PATTERNS_FILE"
TESTS = pathlib.Path(__file__).resolve().parent


def local_leak_patterns_file() -> pathlib.Path:
    if os.environ.get(LEAK_PATTERNS_ENV):
        return pathlib.Path(os.environ[LEAK_PATTERNS_ENV]).expanduser()
    config = os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config"
    return pathlib.Path(config) / "tt-project" / "leak-patterns.txt"


def load_leak_patterns(path: pathlib.Path) -> list:
    patterns = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error as e:
            raise AssertionError(f"{path}:{n}: malformed leak pattern: {e}") from None
    return patterns


# The public branch and tag teammates install from (README ## Dogfood install). Their names come from
# the branch owner's handle, which local leak patterns may list; only these exact refs are exempt.
PUBLISHED_REFS = ("smartonTT/tt-project-dogfood-1", "smartonTT/tt-project")


def published_plugin_names() -> set:
    """Plugins this marketplace publishes. Their names are public even when a local project
    shares one, so a local pattern matching exactly such a name is not a leak."""
    data = json.loads((PLUGIN.parents[1] / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    return {p["name"].lower() for p in data["plugins"]}


def assert_no_leaks(patterns: list, files, allow=frozenset()) -> None:
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for ref in PUBLISHED_REFS:
            text = text.replace(ref, "")
        for pattern in patterns:
            m = next((m for m in pattern.finditer(text) if m.group(0).lower() not in allow), None)
            assert not m, f"{f}: contains {m.group(0)!r}"


def plugin_text_files(root: pathlib.Path = PLUGIN):
    return [f for f in sorted(root.rglob("*"))
            if f.is_file() and f.suffix in {".py", ".md", ".json", ".js", ".html", ".css", ""}]


def frontmatter(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: no frontmatter"
    return yaml.safe_load(text[3:text.find("\n---", 3)]) or {}


def test_manifests_agree():
    data = [json.loads(p.read_text()) for p in MANIFESTS]
    assert {d["name"] for d in data} == {"tt-project"}
    assert len({d["version"] for d in data}) == 1
    runtime_version = re.search(r'__version__ = "([^"]+)"',
                                (PLUGIN / "runtime" / "ttp" / "__init__.py").read_text()).group(1)
    assert runtime_version == data[0]["version"], "bump runtime/ttp/__init__.py with the manifests"
    assert all(d["skills"] == "./skills/" for d in data)


def test_skills_are_discoverable_and_self_contained():
    skills = sorted(SKILLS.glob("*/SKILL.md"))
    assert {p.parent.name for p in skills} == {"tt-project", "tt-project-harness", "setup-box"}
    for path in skills:
        fm = frontmatter(path)
        assert fm["name"] == path.parent.name
        # The one exception: setup-box, shown as tt-project:setup-box (charter Policies, user msg #548).
        assert fm["name"].startswith("tt-") or fm["name"] == "setup-box", f"{path}: skill names start with tt-"
        assert len(fm["description"]) > 40
    for md in SKILLS.rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        assert not re.search(r"[`(]\.\./\w", text), f"{md}: relative ../ path"
        for ref in re.findall(r"`([\w-]+\.md)`", text):
            if (md.parent / ref).exists() or ref in {"MEMORY.md", "CHARTER.md", "README.md"}:
                continue
            if ref.startswith(("kind-", "daily-review", "coordinator", "worker")):
                continue   # prompt templates of a project's harness, named for the reader
            raise AssertionError(f"{md}: references missing sibling {ref}")


def test_claude_code_shortcut_is_plain_tt_project_and_loads_the_plugin_skill():
    """Claude Code namespaces plugin skills (`/tt-project:tt-project`); `ttp setup` adds a personal
    `/tt-project` that only the user invokes and that loads the plugin's main skill by its full name."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ttp_shortcut", RUNTIME / "shortcut.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fm = yaml.safe_load(mod.TEXT.split("---")[1])
    assert fm["name"] == "tt-project" and fm["name"].startswith("tt-")
    assert fm["disable-model-invocation"] is True, "the model must see one tt-project skill, not two"
    plugin = json.loads(MANIFESTS[0].read_text())["name"]
    assert f"`{plugin}:{fm['name']}`" in mod.TEXT and (SKILLS / fm["name"] / "SKILL.md").exists()
    assert "$ARGUMENTS" in mod.TEXT and mod.MARK in mod.TEXT
    readme = " ".join((PLUGIN / "README.md").read_text(encoding="utf-8").split())
    assert "`/tt-project`" in readme and "`/tt-project:tt-project-harness`" in readme


def test_readme_dogfood_install_pins_the_tag_with_each_hosts_ref_syntax():
    """Teammates install from the dogfood tag, not `main` (which has no tt-project): Claude Code
    takes the ref after `#`, Codex after `--ref`, Cursor through a git checkout; the marketplace
    and plugin names must be the ones both catalogues register."""
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    assert "\n## Dogfood install\n" in readme
    section = readme.split("\n## Dogfood install\n", 1)[1].split("\n## ", 1)[0]
    repo = PLUGIN.parents[1]
    names = {json.loads((repo / f).read_text())["name"] for f in
             (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json")}
    assert len(names) == 1
    market, plugin, tag = names.pop(), json.loads(MANIFESTS[0].read_text())["name"], PUBLISHED_REFS[0]
    assert f"claude plugin marketplace add 'https://github.com/tenstorrent/skills.git#{tag}'" in section
    assert f"codex plugin marketplace add tenstorrent/skills --ref {tag}" in section
    assert f"git clone --depth 1 --branch {tag} " in section
    for cmd in (f"claude plugin install {plugin}@{market}", f"codex plugin add {plugin}@{market}",
                f"claude plugin marketplace remove {market}", f"codex plugin marketplace remove {market}",
                "bin/ttp\" setup", "ttp new ", "ttp stop <name> --kill"):
        assert cmd in section, cmd


def test_runtime_is_standard_library_only_and_python39_compatible():
    stdlib = set(sys.stdlib_module_names)
    for py in RUNTIME.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        tree = ast.parse(src, feature_version=(3, 9))   # rejects syntax newer than 3.9
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for n in names:
                assert n in stdlib or n == "ttp", f"{py}: imports non-stdlib module {n}"
        if re.search(r"\w\]?\s*\|\s*None\b", src):
            assert "from __future__ import annotations" in src, f"{py}: PEP 604 unions need the future import on 3.9"


def test_no_secrets_or_personal_paths():
    assert_no_leaks([re.compile(p) for p in FORBIDDEN], plugin_text_files())
    # The plugin's tests ship in the same open-source repository. This file names the patterns
    # themselves (as regex source), so it is checked with that list cut out.
    assert_no_leaks([re.compile(p) for p in FORBIDDEN], [TESTS / "test_tt_project_runtime.py"])
    own = (TESTS / "test_tt_project_plugin.py").read_text(encoding="utf-8")
    own = own[:own.index("FORBIDDEN = [")] + own[own.index("]\n", own.index("FORBIDDEN = [")):]
    for pattern in map(re.compile, FORBIDDEN):
        m = pattern.search(own)
        assert not m, f"tests/test_tt_project_plugin.py: contains {m.group(0)!r}"
    for name in ("A.B-2", "ab"):   # built here so this file holds no such path itself
        assert re.search(FORBIDDEN[-1], "/" + "Users/" + name + "/x"), name


def test_no_local_leak_patterns_in_plugin():
    path = local_leak_patterns_file()
    if not path.is_file():
        pytest.skip(f"setup-specific leak check skipped: no local pattern file at {path} "
                    f"(set {LEAK_PATTERNS_ENV} or create it)")
    patterns = load_leak_patterns(path)
    assert_no_leaks(patterns, plugin_text_files() + sorted(TESTS.glob("test_tt_project_*.py")),
                    allow=published_plugin_names())


def test_local_leak_patterns_are_found_and_checked(tmp_path, monkeypatch):
    monkeypatch.setenv(LEAK_PATTERNS_ENV, str(tmp_path / "patterns.txt"))
    assert local_leak_patterns_file() == tmp_path / "patterns.txt"
    monkeypatch.delenv(LEAK_PATTERNS_ENV)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert local_leak_patterns_file() == tmp_path / "cfg" / "tt-project" / "leak-patterns.txt"

    tree = tmp_path / "plugin"
    (tree / "docs").mkdir(parents=True)
    (tree / "docs" / "notes.md").write_text("runs on Example-Box-7 every night\n")
    patterns_file = tmp_path / "patterns.txt"
    patterns_file.write_text("# planted\n\n(?i)example-box-\\d\n")
    patterns = load_leak_patterns(patterns_file)
    assert len(patterns) == 1
    with pytest.raises(AssertionError, match="Example-Box-7"):
        assert_no_leaks(patterns, plugin_text_files(tree))
    (tree / "docs" / "notes.md").write_text("runs on a build box every night\n")
    assert_no_leaks(patterns, plugin_text_files(tree))

    owner = PUBLISHED_REFS[0].split("/")[0]
    owner_patterns = [re.compile(re.escape(owner[:4]))]
    (tree / "docs" / "notes.md").write_text(f"install from {PUBLISHED_REFS[0]} or {PUBLISHED_REFS[1]}\n")
    assert_no_leaks(owner_patterns, plugin_text_files(tree))
    (tree / "docs" / "notes.md").write_text(f"install from {owner}/elsewhere\n")
    with pytest.raises(AssertionError, match="contains"):
        assert_no_leaks(owner_patterns, plugin_text_files(tree))

    # A published plugin's exact name is allowed; the same pattern still catches anything longer.
    (tree / "docs" / "notes.md").write_text("see the tt-buddy plugin\n")
    buddy = [re.compile(r"(?i)tt-buddy\w*")]
    assert "tt-buddy" in published_plugin_names()
    assert_no_leaks(buddy, plugin_text_files(tree), allow=published_plugin_names())
    (tree / "docs" / "notes.md").write_text("see the tt-buddy plugin, not tt-buddyhost\n")
    with pytest.raises(AssertionError, match="tt-buddyhost"):
        assert_no_leaks(buddy, plugin_text_files(tree), allow=published_plugin_names())

    patterns_file.write_text("fine\n(unclosed\n")
    with pytest.raises(AssertionError, match=r"patterns\.txt:2: malformed leak pattern"):
        load_leak_patterns(patterns_file)


def test_launchers_are_executable():
    import os
    for launcher in (PLUGIN / "bin" / "ttp", PLUGIN / "template" / "bin" / "ttp"):
        assert os.access(launcher, os.X_OK), f"{launcher} is not executable"
    assert (PLUGIN / "bin" / "ttp").read_text() == (PLUGIN / "template" / "bin" / "ttp").read_text()


def test_harness_tasks_stay_in_their_own_harness():
    """A harness task never edits the plugin's source or another project's harness; generic lessons
    travel as upstream notes in the hand-off instead."""
    prompts = PLUGIN / "template" / "prompts"
    for path in (prompts / "kind-harness.md", prompts / "worker.md", SKILLS / "tt-project-harness" / "SKILL.md"):
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "only this project's own harness" in text, f"{path.name}: harness tasks stay in their own harness"
        assert re.search(r"[Nn]ever edit[s]?, or (create|make)s? a worktree or branch in, the tt-project "
                         r"plugin's source repository or (any other|another) project's harness", text), path.name
        assert "upstream note" in text and "`upstream: " in text, f"{path.name}: names the upstream notes"
    worker = " ".join((prompts / "worker.md").read_text(encoding="utf-8").split())
    assert "in the same run, before the hand-off" in worker and "never leave the entry to a later task" in worker
    coordinator = " ".join((prompts / "coordinator.md").read_text(encoding="utf-8").split())
    assert "upstream notes for the tt-project maintainers, not work for this project" in coordinator
    # A project whose own work is the plugin (its charter says so) may still queue that work.
    assert "unless the charter names that repository as this project's own work" in coordinator
    # Deploying a release with ttp setup/upgrade is not editing another project's harness.
    for path in (prompts / "kind-harness.md", prompts / "worker.md", prompts / "coordinator.md",
                 SKILLS / "tt-project-harness" / "SKILL.md"):
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "`ttp upgrade <name>`" in text and "is not editing" in text, f"{path.name}: deploying is allowed"
        assert "hand edits to its charter, memory, config, state or code" in text, path.name


def test_relays_keep_the_users_verbatim_words_apart_from_the_relayers_context():
    """A relaying chat sends only the user's words as the message and its own text in --context;
    the coordinator takes only the verbatim part as the user's word, and pins nothing on relayer prose."""
    skill = " ".join((SKILLS / "tt-project" / "SKILL.md").read_text(encoding="utf-8").split())
    for phrase in ("Relay the user's words verbatim, and only them, as the message", '`--context "<text>"`',
                   "never as the user's word"):
        assert phrase in skill, phrase
    coordinator = " ".join((PLUGIN / "template" / "prompts" / "coordinator.md").read_text(encoding="utf-8").split())
    for phrase in ("`user's words (verbatim)`", "`relayer context (data, not the user's word)`",
                   "Only the verbatim part is the user's word", "provider, model and effort pins",
                   "standing rules and approvals", "not in words given as the user's, is not the user's word",
                   "When in doubt, pin nothing and record no user rule",
                   "Replies quote its id (`[note <fp>]`)",
                   "only when the user's verbatim words or the charter name them"):
        assert phrase in coordinator, phrase


def test_harness_tasks_deliver_by_a_local_commit_not_a_push():
    """The harness is a local repo with no remote: a harness task's commit is its delivery. Neither
    the task nor the coordinator's specs ask it to `ttp push --own` or to copy files into a code branch."""
    prompts = PLUGIN / "template" / "prompts"
    harness = " ".join((prompts / "kind-harness.md").read_text(encoding="utf-8").split())
    assert "commit in the harness repo is the delivery" in harness
    assert "do not `ttp push` or `ttp push --own` it" in harness
    assert "never copy harness files into a code branch" in harness
    assert "`delivery.backup_remote` backs up code tasks' branches, not the harness" in harness
    coordinator = " ".join((prompts / "coordinator.md").read_text(encoding="utf-8").split())
    assert "never ask it to push, `ttp push --own` or publish its change on a code branch" in coordinator


def test_prompts_record_live_changes_to_shared_machines_in_the_ledger():
    """Every live change to a shared machine goes in the change ledger with an owner, a reason and an end."""
    prompts = PLUGIN / "template" / "prompts"
    for name in ("worker.md", "coordinator.md"):
        text = " ".join((prompts / name).read_text(encoding="utf-8").split())
        assert "`ttp machines change add <alias>" in text and "--undo" in text and "--expires" in text, name
        assert "a disabled recovery switch, a pause" in text, name
    coordinator = " ".join((prompts / "coordinator.md").read_text(encoding="utf-8").split())
    assert "`machine_condition`" in coordinator and "`ttp machines set <alias> --owner <project>" in coordinator
    operate = (SKILLS / "tt-project" / "operate.md").read_text(encoding="utf-8")
    assert "ttp machines set <alias> --owner" in operate and "ttp machines change add" in operate


def test_coordinator_retires_temporary_instructions_and_does_not_ask_needlessly():
    """Temporary words get an end; a clearly-over restriction is retired, not asked about; a known,
    safe, reversible fix is done and reported, never asked about with a yes recommendation."""
    coordinator = " ".join((PLUGIN / "template" / "prompts" / "coordinator.md").read_text(encoding="utf-8").split())
    for phrase in ("`expires`", "`until`", "`until_probe`", "retire it yourself", "`over`",
                   "never send an ask whose recommendation is yes", "possibly over"):
        assert phrase in coordinator, phrase
    daily = (PLUGIN / "template" / "prompts" / "daily-review.md").read_text(encoding="utf-8")
    assert "stale restriction" in daily and "`replaces`" in daily


def test_coordinator_relays_accepted_reviews_without_a_worker():
    """An accepted review reaches its consumers by a spec-only task_update; no relay-only worker run."""
    coordinator = " ".join((PLUGIN / "template" / "prompts" / "coordinator.md").read_text(encoding="utf-8").split())
    start = coordinator.index("A review that passes is evidence")
    rule = coordinator[start:coordinator.index(" - ", start)]
    for phrase in ("spec-only `task_update`", "accepted commit", "hash-pinned manifest", "verdict",
                   "gates still open", "blocked reason, dependencies and deferral stay",
                   "Never requeue or add a task only to relay a verdict",
                   "Changed source or missing validation is real work", "cannot reach stays blocked",
                   "only queued locally (exit 0) is not delivered"):
        assert phrase in rule, phrase


def test_coordinator_rewrites_a_changed_restriction_in_place():
    """A restriction the user changes is edited in Restrictions in that turn, never left standing
    next to a new section that says otherwise: workers obey the Restrictions block verbatim."""
    coordinator = " ".join((PLUGIN / "template" / "prompts" / "coordinator.md").read_text(encoding="utf-8").split())
    start = coordinator.index("The user changes, narrows, widens or lifts a restriction")
    rule = coordinator[start:coordinator.index(" - ", start)]
    for phrase in ("in that same turn", "`quote` set to the old item", "`replaces`", "no `over`",
                   "Never leave the old item standing next to a new section",
                   "A temporary loosening of a permanent item also `quote`s that item"):
        assert phrase in rule, phrase
    # Next to the other charter_update guidance, under User instructions.
    assert coordinator.index("# User instructions") < start < coordinator.index("# Keep moving")
    assert "merges the permanent Restrictions sections into one block" in rule
    assert "full heading or number" in coordinator and "`Charter sections` line" in coordinator
    start = coordinator.index("If that change is rejected")
    carry = coordinator[start:coordinator.index(" - ", start)]
    for phrase in ("the user's yes stays on record", "without asking again", "same section, target and text",
                   "`coordinator.charter_approval_days`"):
        assert phrase in carry, phrase


# Wording that makes the user do, or approve, what tt-project can do itself.
ASKS_USER_TO_DO = [
    r"\b(ask|tell)(ing)? (the )?user to (run|do|open|install|type|start|restart|set up|enable)\b",
    r"\bwith the user's (OK|okay|approval)\b", r"\bafter the user agrees\b", r"\(ask first\)",
    r"\b(would|do) you (like|want) me to\b", r"\bshall I\b", r"\bwant me to\b", r"\byou can run\b",
    r"\boffer (to|`ttp)", r"\bplease run\b",
]


def user_facing_texts():
    for f in sorted(SKILLS.rglob("*.md")) + sorted((PLUGIN / "template").rglob("*.md")):
        yield f.relative_to(PLUGIN), f.read_text(encoding="utf-8")
    # Strings the runtime prints or shows (status, web app, alerts, help).
    for f in sorted(RUNTIME.rglob("*.py")) + sorted((RUNTIME / "web").glob("*.js")):
        yield f.relative_to(PLUGIN), f.read_text(encoding="utf-8")


def test_no_wording_asks_the_user_to_do_what_tt_project_can_do():
    """Never ask the user to do what tt-project can do, never offer it: do it and say so. A rule that
    forbids such wording may quote it, so items saying "never" and quoted examples are skipped."""
    pattern = re.compile("|".join(ASKS_USER_TO_DO), re.I)
    for rel, text in user_facing_texts():
        # Markdown: one item per paragraph, list entry or table row, so a quote wrapped over lines
        # stays whole. Code: one item per line.
        md = rel.suffix == ".md"
        for item in re.split(r"\n\s*\n|\n(?=\s*[-|#] )", text) if md else text.splitlines():
            item = " ".join(item.split())
            if re.search(r"\bnever\b", item, re.I):
                continue
            if md:   # in code, quotes are the strings themselves
                item = re.sub(r'"[^"]*"', "", item)
            m = pattern.search(item)
            assert not m, f"{rel}: {m.group(0)!r} makes the user do or approve what tt-project can do: {item[:120]}"


def test_local_web_forwards_ask_once_and_exposing_tunnels_ask():
    remote = " ".join((SKILLS / "tt-project" / "remote.md").read_text(encoding="utf-8").split())
    assert "ALWAYS ask the user before opening any tunnel" not in remote
    assert "the user's first one is asked once, per user" in remote and "`ttp consent tunnel yes|no`" in remote
    assert "After a yes, open it and keep it up without asking" in remote and "After a no, never re-ask" in remote
    assert "`ttp web <name> --tunnel --keep`" in remote and "`com.tt-project.tunnel.<name>`" in remote
    assert re.search(r"Ask the user before any tunnel that exposes their machine to others: a reverse forward", remote)
    rows = {row.split("|")[1].strip(): row.split("|")[3].strip()
            for row in (SKILLS / "tt-project" / "remote.md").read_text(encoding="utf-8").splitlines()
            if row.startswith("| ") and "forward" in row}
    assert rows["Laptop reaches a service on a box"] == "no"
    assert rows["Box reaches a service on the laptop"] == "yes"
    assert rows["Box A reaches box B via the laptop"] == "yes"
    skill = (SKILLS / "tt-project" / "SKILL.md").read_text(encoding="utf-8")
    assert "Tunnels need the user's OK first" not in skill and "--tunnel --keep" in skill
    for prompt in ("coordinator.md", "worker.md"):
        text = " ".join((PLUGIN / "template" / "prompts" / prompt).read_text(encoding="utf-8").split())
        assert re.search(r"Never ask (or tell )?the user to do what (you or )?the project can do", text), prompt


def test_new_and_connect_reply_with_a_verified_web_link():
    """After `new` and `connect` the chat replies with the web app link those commands checked, next
    to the locator line, and the relay never gives a link that was not checked."""
    skill = " ".join((SKILLS / "tt-project" / "SKILL.md").read_text(encoding="utf-8").split())
    row = next(r for r in (SKILLS / "tt-project" / "SKILL.md").read_text(encoding="utf-8").splitlines()
               if r.startswith("| 3c."))
    assert "`tt-project://…` line and the verified web app link" in row
    assert "Never give an unverified web app link." in skill and "NOT AVAILABLE" in skill
    assert "show the `tt-project://…` line once, with the verified web app link" in skill
    create = " ".join((SKILLS / "tt-project" / "create.md").read_text(encoding="utf-8").split())
    assert "verified web app link" in create and "give no link" in create
    remote = " ".join((SKILLS / "tt-project" / "remote.md").read_text(encoding="utf-8").split())
    assert "open the same kept forward themselves" in remote


def test_review_lists_every_blocking_finding_and_fixes_test_each_bug_class():
    """One review round finds all blocking bugs; the fix tests each class, not one case."""
    prompts = PLUGIN / "template" / "prompts"
    review = " ".join((prompts / "kind-review.md").read_text(encoding="utf-8").split())
    assert "Find every blocking problem in one pass: after the first, keep going through the whole changed module" in review
    for edge in ("failure paths", "retries", "time windows", "restarts"):
        assert edge in review, edge
    code = " ".join((prompts / "kind-code.md").read_text(encoding="utf-8").split())
    assert "test each class of bug found, not only the reported case" in code


def test_worker_prompt_limits_search_scope():
    text = " ".join((PLUGIN / "template" / "prompts" / "worker.md").read_text(encoding="utf-8").split())
    assert "Never search / or the home folder" in text
    for cmd in ("`find /`", "`find ~`", "`grep -r ~`", "`mdfind`", "git ls-files"):
        assert cmd in text, cmd


def test_shipped_prompts_and_templates_hold_no_one_users_cluster_policy():
    # One user's rules (a node list, an idle threshold, a machine list) belong in their own charter;
    # the shipped prompts, templates and skills state only company-wide scheduler etiquette.
    personal = re.compile(r"(?i)exact nodes|idle (at least|as long)|LastBusyTime|default 2 ?h|nodes? the user "
                          r"(listed|allows)|existing machine list|demo nodes?|any idle node|"
                          r"admins cancel|owner in public|canary|live traffic")
    files = [f for d in ("template", "skills") for f in sorted((PLUGIN / d).rglob("*")) if f.is_file()]
    hits = [f"{f.relative_to(PLUGIN)}:{n}: {line.strip()}" for f in files if f.suffix in (".md", ".json", ".txt")
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1) if personal.search(line)]
    assert not hits, "\n".join(hits)

    def flat(*parts):
        return " ".join((PLUGIN / pathlib.Path(*parts)).read_text(encoding="utf-8").split())
    worker = flat("template", "prompts", "worker.md")
    for part in ("Get nodes only through the cluster's scheduler.",
                 "Where the charter names nodes, partitions or limits, stay within them",
                 "never widen your own scope", "allocated, drained, reserved for others or down",
                 "Release the allocation as soon as the work ends."):
        assert part in worker, part
    coord = flat("template", "prompts", "coordinator.md")
    assert "within any nodes, partitions or limits the charter names" in coord
    assert "never widen a task's scope yourself" in coord
    assert "within any nodes, partitions or limits under Resources" in flat("template", "CHARTER.md")
    assert "add none of your own" in flat("skills", "tt-project", "create.md")


def test_prompts_and_charter_template_say_self_heal_then_report():
    def flat(*parts):
        return " ".join((PLUGIN / "template" / pathlib.Path(*parts)).read_text(encoding="utf-8").split())
    coord = flat("prompts", "coordinator.md")
    for part in ("Self-healing: keeping the project stable is yours.", "a hold that never cleared",
                 "(`task_add` priority 1) or a heal check", "notify at severity `low` afterwards",
                 "`ttp note --to <project>`", "`classify`", "`restriction_change`",
                 "`neither`, which is rejected: decide it yourself", "name the reversible alternative you considered",
                 "The class must match `blocking`", "a step only the user can take (physical or on site",
                 "another person's request", "paraphrases get through it", "heal checks cover what it misses"):
        assert part in coord, part
    worker = flat("prompts", "worker.md")
    assert ("is fixed in the task, or handed off with a fix plan in `followups`; never handed off `blocked` as a "
            "question about what to do") in worker
    charter = flat("CHARTER.md")
    assert ("Self-healing: keeping the project stable is a prime directive; check everything it is responsible "
            "for, fix anomalies yourself and report afterwards.") in charter


def test_worker_prompt_bounds_disk_usage_scans():
    text = " ".join((PLUGIN / "template" / "prompts" / "worker.md").read_text(encoding="utf-8").split())
    for part in ("`df <path>`", "Never `du` from / or a home folder", "`du -x`", "`timeout`", "is unknown, not a blocker"):
        assert part in text, part


def test_worker_prompt_quotes_log_tail_before_diagnosing_a_failed_job():
    text = " ".join((PLUGIN / "template" / "prompts" / "worker.md").read_text(encoding="utf-8").split())
    for part in ("exit code alone never names its cause", "quote its last 20 log lines", "first error line", "base the cause on them"):
        assert part in text, part


def test_prompts_require_home_zone_times_with_abbreviation_never_utc():
    prompts = PLUGIN / "template" / "prompts"
    worker = " ".join((prompts / "worker.md").read_text(encoding="utf-8").split())
    for part in ("Every time you write for the user (notes, notify, hand-off summary, drafts, reports)",
                 "home zone from your task header, with its abbreviation (e.g. `09:30 CET`)",
                 "never the host's, UTC or `Z`", "convert times you copy from UTC logs"):
        assert part in worker, part
    coord = " ".join((prompts / "coordinator.md").read_text(encoding="utf-8").split())
    for part in ("show the user every time in the project's home zone",
                 "with its abbreviation (e.g. `09:30 CET`), converting UTC or `Z` ones"):
        assert part in coord, part
    review = " ".join((prompts / "daily-review.md").read_text(encoding="utf-8").split())
    assert "times in the home zone and its abbreviation, never UTC" in review


def test_worker_prompt_asks_for_narrow_file_reads():
    text = " ".join((PLUGIN / "template" / "prompts" / "worker.md").read_text(encoding="utf-8").split())
    for part in ("`grep -n` first", "only the ranges you need", "Do not re-read a range you already read"):
        assert part in text, part


def test_worker_prompt_ci_wait_wakes_on_a_hung_job():
    text = " ".join((PLUGIN / "template" / "prompts" / "worker.md").read_text(encoding="utf-8").split())
    for part in ("`ttp ci --repo <owner/repo> --branch <branch>`", "exits 0 when a job hangs", "`hung:` line",
                 "`--no-hang`"):
        assert part in text, part


def test_web_js_parses_on_old_node():
    """The web app must stay ES2019 so older system node and browsers can run it."""
    import shutil
    import subprocess
    files = sorted((RUNTIME / "web").glob("*.js"))
    assert files
    for f in files:
        text = f.read_text()
        bad = [m.group(0) for m in re.finditer(r"\?\?|\?\.(?!\d)", text)]
        assert not bad, f"{f.name}: ES2020 syntax {bad} (use == null checks)"
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not installed")
    for f in files:
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name}: {r.stderr}"


# A path token ending in harness/bin made only of literal relative segments: `harness/bin/ttp`,
# `tt-project/harness/bin/ttp`, `./harness/bin`. A token anchored on a root (`/abs/...`, `~/...`,
# `$TTP_PROJECT/...`, `${X}/...`, `{dir}/{FOLDER}/...`, `%s/...`) does not match, and neither does
# code such as `p.harness / "bin"`.
ROOT_RELATIVE_HARNESS_BIN = re.compile(r"(?<![\w$}/.%~-])(?:[\w.-]+/)*harness/bin\b")
# (path relative to the plugin, exact line text stripped) -> why the line may say it. Keep it short.
ROOT_RELATIVE_HARNESS_BIN_ALLOW: dict[tuple[str, str], str] = {}


SKIP_BUILD_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", ".git"}


def shipped_inventory(root=PLUGIN):
    """(source, relative paths) of the plugin's shipped files: `git ls-files` when the plugin is
    tracked in a git checkout (the same check as cli._checkout_commit), else a walk of the
    directory without caches, *.pyc and build output (an exported tree, a copy, no git)."""
    import os
    import subprocess

    def git(*args):
        try:
            r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout if r.returncode == 0 else ""
    if git("ls-files", "--", "runtime/ttp/__init__.py").strip():
        return "git ls-files", sorted(filter(None, git("ls-files", "-z", "--", ".").split("\0")))
    rels = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_BUILD_DIRS and not d.endswith(".egg-info")]
        for f in filenames:
            if not f.endswith((".pyc", ".pyo")):
                rels.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
    return "directory walk", sorted(rels)


def shipped_text_files(root=PLUGIN):
    """Shipped plugin files that decode as text: prompts, skills, runtime .py and scripts."""
    for rel in shipped_inventory(root)[1]:
        path = root / rel
        if not path.is_file():
            continue
        try:
            yield rel, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def test_shipped_inventory_without_git(tmp_path):
    import shutil
    copy = tmp_path / "tt-project"
    shutil.copytree(PLUGIN, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    (copy / "runtime" / "ttp" / "__pycache__").mkdir(exist_ok=True)
    (copy / "runtime" / "ttp" / "__pycache__" / "cli.cpython-312.pyc").write_bytes(b"\0")
    source, rels = shipped_inventory(copy)
    assert source == "directory walk"
    assert "runtime/ttp/cli.py" in rels and "template/bin/ttp" in rels
    assert not [r for r in rels if "__pycache__" in r or r.endswith(".pyc")]


def test_root_relative_harness_bin_pattern():
    for line in ("run tt-project/harness/bin/ttp note x", "`harness/bin/ttp lock`",
                 'cmd = "tt-project/harness/bin/ttp"', "./harness/bin/gh", "x=harness/bin"):
        assert ROOT_RELATIVE_HARNESS_BIN.search(line), line
    for line in ('"$TTP_PROJECT/harness/bin/ttp" note', "${TTP_PROJECT}/harness/bin/ttp",
                 "f\"{entry['dir']}/{FOLDER}/harness/bin/ttp\"", "f\"{root}/tt-project/harness/bin\"",
                 "/abs/proj/tt-project/harness/bin/ttp", "~/proj/tt-project/harness/bin",
                 "'%s/harness/bin' % root", 'ttp = p.harness / "bin" / "ttp"',
                 "f\"{self.p.harness / 'bin'}:{path}\"", "<project>/harness/bin/ttp",
                 "harness/binary"):
        assert not ROOT_RELATIVE_HARNESS_BIN.search(line), line


def test_shipped_text_never_uses_root_relative_harness_bin_paths():
    """Workers run in tt-project/worktrees/<task>, where `tt-project/harness/bin/...` does not
    resolve; shipped prompts, skills, runtime strings and scripts must use
    "$TTP_PROJECT/harness/bin/..." or a path anchored on the project root instead."""
    source, _ = shipped_inventory()
    files = dict(shipped_text_files())
    # The scan must reach runtime code and suffixless scripts, not only prompt files.
    missing = [r for r in ("runtime/ttp/cli.py", "template/bin/ttp") if r not in files]
    assert not missing, f"shipped-file inventory from {source} of {PLUGIN} lacks {missing} ({len(files)} files)"
    hits = []
    for rel, text in files.items():
        for n, line in enumerate(text.splitlines(), 1):
            if ROOT_RELATIVE_HARNESS_BIN.search(line) and \
                    (rel, line.strip()) not in ROOT_RELATIVE_HARNESS_BIN_ALLOW:
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "root-relative harness/bin paths:\n" + "\n".join(hits)


def test_worker_prompt_driver_chain_fails_fast_and_shared_watchers_lock():
    root = PLUGIN
    worker = (root / "template" / "prompts" / "worker.md").read_text()
    assert "pipefail" in worker and "first non-zero" in worker
    harness = (root / "skills" / "tt-project-harness" / "SKILL.md").read_text()
    assert "flock -n" in harness and "condition key" in harness


def test_worker_prompt_driver_marker_uses_exit_trap():
    text = (PLUGIN / "template" / "prompts" / "worker.md").read_text()
    assert "EXIT trap" in text
    assert "trap 'echo $? > \"$marker\"' EXIT" in text


def test_docs_and_prompts_name_only_real_ttp_subcommands():
    """A prompt or skill that names a `ttp <word>` the CLI lacks sends workers to an invalid choice."""
    sys.path.insert(0, str(RUNTIME.parent))
    try:
        from ttp import cli
        import argparse
        seen: list = []
        real = argparse.ArgumentParser.parse_args

        def grab(self, args=None, namespace=None):
            seen.append(self)
            raise SystemExit(0)
        argparse.ArgumentParser.parse_args = grab
        try:
            cli.main(["status"])
        except SystemExit:
            pass
        finally:
            argparse.ArgumentParser.parse_args = real
    finally:
        sys.path.remove(str(RUNTIME.parent))
    sub = next(a for a in seen[0]._actions if isinstance(a, argparse._SubParsersAction))
    commands = set(sub.choices)
    assert {"note", "notify", "push"} <= commands
    files = [f for f in PLUGIN.rglob("*") if f.suffix in (".md", ".py", ".json", ".sh") and f.is_file()]
    bad = sorted({f"{f.relative_to(PLUGIN)}: ttp {m}"
                  for f in files for m in re.findall(r"`ttp ([a-z][a-z-]*)", f.read_text(encoding="utf-8", errors="replace"))
                  if m not in commands})
    assert not bad, bad


def test_worker_prompt_covers_pattern_kills_in_scripts_it_runs():
    text = " ".join((PLUGIN / "template" / "prompts" / "worker.md").read_text(encoding="utf-8").split())
    for part in ("scripts and test stubs you run", "`ttp killscan <script>`", "`ttp killscan --shim <dir>`",
                 "`kill $(pgrep -f ...)`", "a separate session or process group does not protect it"):
        assert part in text, part


def test_suite_keeps_pytest_temp_dirs_small(pytestconfig):
    # The runtime suite makes hundreds of thousands of files under the pytest temp root; on a shared
    # host the default (keep every tmp_path of the last 3 sessions) fills /tmp and slows boot cleanup.
    assert pytestconfig.getini("tmp_path_retention_policy") == "failed"
    assert int(pytestconfig.getini("tmp_path_retention_count")) <= 1


def test_code_and_review_prompts_keep_full_suite_runs_to_ttp_checks():
    prompts = PLUGIN / "template" / "prompts"
    code = " ".join((prompts / "kind-code.md").read_text(encoding="utf-8").split())
    for part in ("While you iterate, run only the tests your change affects", "never the full suite",
                 "Run `ttp checks` once at the end, committed, before the hand-off",
                 "A direct full-suite run is refused",
                 "each command quoted as one word (`-- 'pytest -q tests/a.py' 'ruff check .'`)"):
        assert part in code, part
    review = " ".join((prompts / "kind-review.md").read_text(encoding="utf-8").split())
    for part in ("Rely on the pass the change's task recorded for the head you review",
                 "They run again only when the head differs or no pass is recorded",
                 "never rerun the full suite by hand"):
        assert part in review, part


def test_the_coordinator_defers_on_a_landing_with_landed_id_not_a_raw_sha():
    """Pushing rebases each commit to a new sha, so a raw-sha ancestor probe can go stale for good."""
    prompts = PLUGIN / "template" / "prompts"
    coordinator = " ".join((prompts / "coordinator.md").read_text(encoding="utf-8").split())
    assert "`start_when` `landed:#<id>`" in coordinator
    assert "Never wait on a raw sha" in coordinator and "`ttp landed <sha>`" in coordinator
    review = " ".join((prompts / "daily-review.md").read_text(encoding="utf-8").split())
    assert "`landed:#<id>`" in review


def test_the_forked_test_runner_reports_every_test_once_as_one_process_would(tmp_path):
    """tests/conftest.py runs the tt-project tests in forked workers: every test runs exactly once,
    failures, skips and warnings come back as in one process, other files stay in one worker in
    order, and a worker that dies fails the test it was running instead of losing it."""
    import shutil
    import subprocess
    shutil.copy(pathlib.Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_tt_project_sample.py").write_text(
        "import os, pathlib, warnings, pytest\n"
        "@pytest.mark.parametrize('i', range(60))\n"
        "def test_ok(i, tmp_path):\n"
        "    pathlib.Path(os.environ['OUT'], f'ok-{i}').write_text(str(os.getpid()))\n"
        "def test_fail(): assert 1 == 2, 'boom'\n"
        "def test_skip(): pytest.skip('not here')\n"
        "def test_warn(): warnings.warn('careful', UserWarning)\n"
        "def test_crash(): os._exit(3)\n")
    (tmp_path / "test_other.py").write_text(
        "import os, pathlib\n"
        "def test_a(): pathlib.Path(os.environ['OUT'], 'a').write_text(str(os.getpid()))\n"
        "def test_b(): assert pathlib.Path(os.environ['OUT'], 'a').read_text() == str(os.getpid())\n")

    def run(jobs):
        out = tmp_path / f"out-{jobs}"
        out.mkdir()
        env = {**os.environ, "TTP_TEST_JOBS": str(jobs), "OUT": str(out)}
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rfEs", "-p", "no:cacheprovider", "-W",
                            "default", str(tmp_path)], cwd=str(tmp_path), env=env, capture_output=True,
                           text=True, timeout=120)
        return r, out

    forked, out = run(3)
    assert forked.returncode == 1, forked.stdout
    assert "2 failed, 63 passed, 1 skipped, 1 warning" in forked.stdout, forked.stdout
    assert "FAILED test_tt_project_sample.py::test_fail - AssertionError: boom" in forked.stdout
    assert "test_tt_project_sample.py::test_crash - the test worker process" in forked.stdout
    assert "SKIPPED [1] test_tt_project_sample.py:" in forked.stdout and "UserWarning: careful" in forked.stdout
    pids = {(out / f"ok-{i}").read_text() for i in range(60)}
    assert len(pids) > 1 and str(os.getpid()) not in pids, "the tests did not run in forked workers"
    # One process runs the same tests the same way, but for the test that ends its process.
    single, _ = run(1)
    assert single.returncode != 0 and "2 failed" not in single.stdout, single.stdout


BOX_PROBE = PLUGIN / "template" / "bin" / "tt-box-clean-probe"
PROBE_NOW = "2026-01-10T12:00:00"


def _box_probe(tmp_path, jobs=None, up_h=100.0, args=(), broker=None, tz="UTC", now=PROBE_NOW):
    """Run the box-clean probe at `now` (local, in zone `tz`) with a fake uptime and either a jobs JSON
    file or a broker."""
    import subprocess
    up = tmp_path / "uptime"
    up.write_text("%.2f 1.00\n" % (up_h * 3600))
    cmd = [sys.executable, str(BOX_PROBE), "--uptime-file", str(up), *(["--now", now] if now else []), *args]
    if jobs is not None:
        f = tmp_path / "jobs.json"
        f.write_text(json.dumps(jobs))
        cmd += ["--jobs-file", str(f)]
    else:
        cmd += ["--broker-python", str(broker or tmp_path / "no-broker-python")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=dict(os.environ, TZ=tz))
    return r.returncode, r.stdout.strip()


def _job(hours_ago, owner="alice", status="done", command="pytest tests", job_id="j1"):
    import datetime as dt
    t = dt.datetime.fromisoformat(PROBE_NOW) - dt.timedelta(hours=hours_ago)
    return {"job_id": job_id, "owner": owner, "status": status, "command": command, "queued_at": t.isoformat()}


def test_box_clean_probe_ships_executable_and_documented():
    assert os.access(BOX_PROBE, os.X_OK)
    assert "tt-box-clean-probe" in (SKILLS / "tt-project-harness" / "SKILL.md").read_text()


@pytest.mark.parametrize("job", [
    _job(3, owner="[broker]power-cycle"), _job(3, owner="[broker]bridge-reset"), _job(3, owner="[broker]reboot"),
    _job(3, owner="[broker]pci-rescan"), _job(3, owner="[broker]health-gate"),
    dict(_job(3, owner="[broker]hold", status="started"), finished_at="2026-01-10T09:00:00"),
    _job(3, owner="[broker]fabric-check", status="failed"),
    _job(3, status="broker-kill"), _job(3, status="hung"), _job(3, command="tt-smi -r 0"),
    _job(3, command="python -c 'tt_device_reset()'"),
])
def test_box_clean_probe_counts_each_incident_kind(tmp_path, job):
    ended = _job(2.5, owner="[broker]hold", status="ended")
    rc, out = _box_probe(tmp_path, {"jobs": [_job(1), job, ended, _job(30, owner="[broker]power-cycle")]})
    assert rc == 1, out
    assert out.startswith("NOT CLEAN: newest incident 2026-01-10 09:00:00 (") and "clean at 2026-01-11 09:00" in out


def test_box_clean_probe_ignores_routine_and_old_jobs(tmp_path):
    jobs = [_job(1, owner="[broker]startup"), _job(2, owner="[broker]fabric-check"),
            _job(3, owner="[broker]hold", status="ended"), _job(4, status="failed"),
            _job(5, command="tt-smi -s"), _job(25, owner="[broker]power-cycle"), {"job_id": "x", "owner": "[broker]reboot"}]
    rc, out = _box_probe(tmp_path, {"jobs": jobs})
    assert (rc, out) == (0, "CLEAN: up 100.0 h, no broker incident since 2026-01-09 12:00")
    # A shorter window, aware timestamps and a bare list are read too.
    aware = [dict(_job(3, owner="[broker]reboot"), queued_at="2026-01-10T11:00:00+01:00")]
    assert _box_probe(tmp_path, aware, args=["--window-h", "2"])[0] == 1
    assert _box_probe(tmp_path, aware, args=["--window-h", "1.5"])[0] == 0
    aware[0]["queued_at"] = "2026-01-10T10:30:00Z"
    assert _box_probe(tmp_path, aware, args=["--window-h", "2"])[0] == 1


def test_box_clean_probe_dates_a_job_by_when_it_went_wrong(tmp_path):
    """A job queued and started before the window but hung, killed or running inside it counts."""
    import datetime as dt
    at = lambda h: (dt.datetime.fromisoformat(PROBE_NOW) - dt.timedelta(hours=h)).isoformat()
    hung = dict(_job(26, status="hung"), started_at=at(25), finished_at=at(1))
    rc, out = _box_probe(tmp_path, {"jobs": [_job(0.5), hung]})
    assert rc == 1 and "newest incident 2026-01-10 11:00:00" in out, out
    running = dict(_job(30, owner="[broker]power-cycle", status="running"), started_at=at(29))
    # A hold whose start row (finished when written) has no later "ended" row is still on.
    held = [dict(_job(50, owner="[broker]hold", status="ended")),
            dict(_job(40, owner="[broker]hold", status="started"), started_at=at(40), finished_at=at(40))]
    for jobs in ([running], held):
        rc, out = _box_probe(tmp_path, {"jobs": [_job(0.5), *jobs]})
        assert rc == 1 and "newest incident 2026-01-10 12:00:00" in out, out
    # A routine job that finished inside the window is still routine; the history check uses its queue time.
    done = dict(_job(30), started_at=at(29), finished_at=at(1))
    assert _box_probe(tmp_path, {"jobs": [done]})[0] == 0
    assert _box_probe(tmp_path, {"jobs": [_job(0.5), done]}, args=["--limit", "2"])[0] == 0


def test_box_clean_probe_counts_a_degraded_hold_and_chip_drop_orphans(tmp_path):
    """The broker's live row for a device held degraded (no tenant refused yet) is a hold on now."""
    import datetime as dt
    at = lambda h: (dt.datetime.fromisoformat(PROBE_NOW) - dt.timedelta(hours=h)).isoformat()
    held = {"job_id": "--", "owner": "[broker]", "status": "running", "finished_at": None,
            "queued_at": at(30), "started_at": at(30),
            "command": "device HELD (degraded): chip 3 off the bus - new tenant jobs refused until recovered"}
    rc, out = _box_probe(tmp_path, {"jobs": [held]}, args=["--window-h", "6"])
    assert rc == 1 and "newest incident 2026-01-10 12:00:00" in out, out
    # A tenant job orphaned by a chip drop counts when it went wrong; a plain interrupted one does not.
    orphan = dict(_job(3, status="interrupted", command="[MCP killed: chips left PCIe] pytest tests"),
                  started_at=at(3), finished_at=at(2))
    rc, out = _box_probe(tmp_path, {"jobs": [orphan]}, args=["--window-h", "6"])
    assert rc == 1 and "newest incident 2026-01-10 10:00:00" in out, out
    plain = dict(orphan, command="[interrupted: reboot] pytest tests")
    assert _box_probe(tmp_path, {"jobs": [plain]}, args=["--window-h", "6"])[0] == 0


def test_box_clean_probe_reads_broker_times_as_local(tmp_path):
    """The broker writes naive local times: west of UTC, an incident 1 h ago is still inside a 6 h window."""
    import datetime as dt
    tz = "America/Los_Angeles"
    if not os.path.exists("/usr/share/zoneinfo/" + tz):
        tz = "PST8PDT"
    env = dict(os.environ, TZ=tz)
    import subprocess
    local_now = subprocess.run([sys.executable, "-c", "import datetime; print(datetime.datetime.now().isoformat())"],
                               capture_output=True, text=True, env=env).stdout.strip()
    hour_ago = (dt.datetime.fromisoformat(local_now) - dt.timedelta(hours=1)).isoformat()
    job = {"job_id": "j9", "owner": "[broker]power-cycle", "status": "completed", "command": "power-cycle",
           "queued_at": hour_ago, "started_at": hour_ago, "finished_at": hour_ago}
    rc, out = _box_probe(tmp_path, {"jobs": [job]}, args=["--window-h", "6"], tz=tz, now=None)
    assert rc == 1, out


def test_box_clean_probe_counts_a_recent_boot_and_short_history(tmp_path):
    rc, out = _box_probe(tmp_path, {"jobs": []}, up_h=5)
    assert rc == 1 and "(host boot)" in out and "clean at 2026-01-11 07:00" in out
    rc, out = _box_probe(tmp_path, {"jobs": [_job(1), _job(2)]}, args=["--limit", "2"])
    assert rc == 1 and "does not reach back 24.0 h" in out
    assert _box_probe(tmp_path, {"jobs": [_job(1), _job(30)]}, args=["--limit", "2"])[0] == 0


def test_box_clean_probe_degrades_without_a_broker_and_255_when_unreadable(tmp_path):
    rc, out = _box_probe(tmp_path)
    assert rc == 0 and "no broker installed, uptime only" in out
    assert _box_probe(tmp_path, up_h=3)[0] == 1
    rc, out = _box_probe(tmp_path, args=["--require-broker"])
    assert rc == 255 and out.startswith("UNKNOWN: no broker python at")
    for body in ("exit 3", "echo not json", "echo '{\"error\": \"held\"}'"):
        fake = tmp_path / "broker-python"
        fake.write_text("#!/bin/sh\n%s\n" % body)
        fake.chmod(0o755)
        rc, out = _box_probe(tmp_path, broker=fake)
        assert rc == 255 and out.startswith("UNKNOWN: broker"), out
    (tmp_path / "jobs.json").write_text("{")
    import subprocess
    r = subprocess.run([sys.executable, str(BOX_PROBE), "--jobs-file", str(tmp_path / "jobs.json"),
                        "--uptime-file", str(tmp_path / "missing")], capture_output=True, text=True)
    assert r.returncode == 255 and r.stdout.startswith("UNKNOWN: uptime unreadable")


def test_box_clean_probe_queries_the_broker_python(tmp_path):
    """The broker's python gets the fetch snippet with port, limit and timeout and its JSON is judged."""
    fake = tmp_path / "broker-python"
    seen = tmp_path / "argv"
    payload = json.dumps({"jobs": [_job(2, owner="[broker]health-gate")]})
    fake.write_text("#!/bin/sh\nprintf '%%s\\n' \"$@\" > %s\necho '%s'\n" % (seen, payload))
    fake.chmod(0o755)
    rc, out = _box_probe(tmp_path, broker=fake, args=["--port", "9000", "--limit", "50"])
    assert rc == 1 and "[broker]health-gate" in out, out
    text = seen.read_text()
    assert text.startswith("-c\n") and "tt_device_recent_jobs" in text and text.endswith("\n9000\n50\n40\n")


def test_self_reboot_recipe_never_waits_on_drain_probe_while_paused():
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-project" / "skills"
    for name in ("tt-project", "tt-project-harness"):
        text = (root / name / "SKILL.md").read_text()
        assert "--wait 240" in text
        assert not re.search(r"retry_when`?\s*`ttp drain", text)
        assert "ttp resume <name>" in " ".join(text.split())


def test_worker_prompt_says_device_hosts_hide_devices_from_direct_opens():
    """On device hosts devices are hidden from direct opens, device work goes only through the broker,
    and a worker never unsets or overrides the masking variables."""
    worker = " ".join((PLUGIN / "template" / "prompts" / "worker.md").read_text(encoding="utf-8").split())
    assert "On a machine tagged `device`, devices are hidden from direct opens (`TT_VISIBLE_DEVICES`" in worker
    assert ("Device work goes only through the device broker, `ttp devq` or `ttp lock` on a device resource."
            in worker)
    assert "Never unset or override the masking variables to work around this." in worker


def test_runs_without_a_device_never_open_one_through_tests():
    """A worker or reviewer that holds no device runs only the task's own test selection, and checks a
    marker exists before deselecting by it; the harness skill ships the opt-in pytest guard."""
    prompts = PLUGIN / "template" / "prompts"
    worker = " ".join((prompts / "worker.md").read_text(encoding="utf-8").split())
    assert "`TTP_NO_DEVICE=1` in your environment means your task holds no device" in worker
    assert "exact test ids or `-k` expression" in worker
    assert "only after `pytest --markers` shows the repo defines it" in worker
    assert "open one only through the device broker, `ttp devq` or `ttp lock` on a device resource" in worker
    # The `ttp lock` bullet stays whole: its exit-75 guidance is not split off by another bullet.
    raw = (prompts / "worker.md").read_text(encoding="utf-8")
    start = raw.index("wrap each command that touches one in `ttp lock")
    end = raw.index("`ttp lock --probe <resource>`", start)
    assert "\n- " not in raw[start:end]
    skill = " ".join((SKILLS / "tt-project-harness" / "SKILL.md").read_text(encoding="utf-8").split())
    assert "TTP_NO_DEVICE=1" in skill and "ttp_no_device.py" in skill
    assert (SKILLS / "tt-project-harness" / "ttp_no_device.py").is_file()


@pytest.mark.parametrize("no_device, fixtures, expect", [
    (None, None, {"test_cpu", "test_dev", "test_indirect", "test_marked"}),
    ("0", None, {"test_cpu", "test_dev", "test_indirect", "test_marked"}),
    ("1", None, {"test_cpu", "test_marked"}),
    ("1", "board_*", {"test_cpu", "test_dev", "test_indirect", "test_marked"}),
])
def test_the_no_device_pytest_plugin_deselects_device_tests_only_under_ttp_no_device(
        tmp_path, no_device, fixtures, expect):
    import subprocess
    import shutil
    shutil.copy(SKILLS / "tt-project-harness" / "ttp_no_device.py", tmp_path / "ttp_no_device.py")
    (tmp_path / "conftest.py").write_text(
        'import pytest\npytest_plugins = ["ttp_no_device"]\n\n'
        '@pytest.fixture\ndef mesh_device():\n    raise RuntimeError("opened a device")\n\n'
        '@pytest.fixture\ndef model(mesh_device):\n    return mesh_device\n')
    (tmp_path / "test_x.py").write_text(
        "import pytest\n\ndef test_cpu():\n    pass\n\ndef test_dev(mesh_device):\n    pass\n\n"
        "def test_indirect(model):\n    pass\n\n@pytest.mark.hw\ndef test_marked():\n    pass\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    hw: needs hardware\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TTP_", "PYTEST_"))}
    if no_device is not None:
        env["TTP_NO_DEVICE"] = no_device
    if fixtures is not None:
        env["TTP_DEVICE_FIXTURES"] = fixtures
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q"],
                       cwd=tmp_path, env=env, capture_output=True, text=True)
    got = {ln.split("::")[1] for ln in r.stdout.splitlines() if "::" in ln}
    assert got == expect, r.stdout + r.stderr
    if no_device == "1" and fixtures is None:
        assert "2 deselected" in r.stdout, r.stdout
        # Markers are configurable too, and an exact id that needs a device is not run.
        r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "test_x.py::test_dev"],
                           cwd=tmp_path, env={**env, "TTP_DEVICE_MARKERS": "hw"}, capture_output=True, text=True)
        assert r.returncode == 5 and "opened a device" not in r.stdout, r.stdout
        assert "ttp_no_device: deselected 1 device test" in r.stdout, r.stdout
        r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q"],
                           cwd=tmp_path, env={**env, "TTP_DEVICE_MARKERS": "hw"}, capture_output=True, text=True)
        assert {ln.split("::")[1] for ln in r.stdout.splitlines() if "::" in ln} == {"test_cpu"}, r.stdout


def test_worker_prompt_detached_driver_retries_transient_faults():
    text = (PLUGIN / "template/prompts/worker.md").read_text()
    assert "Retry transient faults" in text
    assert "bounded backoff" in text
    assert "Prefer `on_pass_cmd` to a light-wake `next_step`" in text


BOX_PY = SKILLS / "setup-box" / "scripts" / "box.py"


def _site_example():
    """The schema example in site.md, which must itself pass `site check`."""
    text = (SKILLS / "setup-box" / "site.md").read_text(encoding="utf-8")
    return json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))


def _box_check(tmp_path, site):
    import subprocess
    path = tmp_path / "boxes.json"
    path.write_text(site if isinstance(site, str) else json.dumps(site), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "TTP_BOXES_FILE"}
    return subprocess.run([sys.executable, str(BOX_PY), "--site", str(path), "site", "check"],
                          capture_output=True, text=True, timeout=30, env=env)


def test_setup_box_site_check_accepts_a_valid_file(tmp_path):
    site = _site_example()
    site["boxes"]["<bare-alias>"] = {"route": "bare", "workdir_root": "/<workdir>"}
    site["boxes"]["<ird-alias>"] = {"route": "ird", "workdir_root": "~/<workdir>",
                                    "ird": {"mgmt": "<ird-mgmt-alias>", "pools": ["<pool>"]}}
    r = _box_check(tmp_path, site)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok <box-alias> route=slurm" in r.stdout and "error" not in r.stdout


@pytest.mark.parametrize("mutate, message", [
    (lambda s: s["boxes"]["<box-alias>"].update(route="cloud"), "route must be one of"),
    (lambda s: s["boxes"]["<box-alias>"].pop("workdir_root"), "workdir_root must be an absolute path"),
    (lambda s: s["boxes"]["<box-alias>"]["slurm"].pop("login"), "slurm.login"),
    (lambda s: s["boxes"]["<box-alias>"]["slurm"]["partitions"][0].update(chips=0), "chips must be"),
    (lambda s: s["boxes"]["<box-alias>"]["slurm"].update(partitions=[]), "non-empty list"),
    (lambda s: s["boxes"]["<box-alias>"].update(route="ird"), "route ird needs an ird object"),
    (lambda s: s["boxes"]["<box-alias>"].update(excluded_nodes="<node>"), "excluded_nodes must be a list"),
    (lambda s: s.update(boxes={}), "boxes must be a non-empty object"),
    (lambda s: s.pop("version"), "version must be 1"),
])
def test_setup_box_site_check_refuses_a_malformed_file(tmp_path, mutate, message):
    site = _site_example()
    mutate(site)
    r = _box_check(tmp_path, site)
    assert r.returncode == 1 and message in r.stdout, r.stdout + r.stderr


def test_setup_box_site_check_refuses_missing_or_broken_json(tmp_path):
    r = _box_check(tmp_path, "{not json")
    assert r.returncode == 1 and "error:" in r.stdout
    import subprocess
    r = subprocess.run([sys.executable, str(BOX_PY), "--site", str(tmp_path / "none.json"), "site", "check"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "no site file" in r.stdout


def test_setup_box_extensions_and_ssh_run(tmp_path, monkeypatch):
    """Route tasks extend box.py through box_<name>.py files; ssh_run is non-interactive."""
    import importlib.util
    import shutil
    import subprocess
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(BOX_PY, scripts / "box.py")
    (scripts / "box_demo.py").write_text(
        "KNOWN_KEYS = {'demo'}\n"
        "CHECKS = {'slurm': lambda alias, e: [] if e.get('demo') else [alias + ': demo missing']}\n"
        "def register(sub):\n"
        "    sub.add_parser('demo').set_defaults(func=lambda args: print('demo ran') or 0)\n")
    site = _site_example()
    path = tmp_path / "boxes.json"
    path.write_text(json.dumps(site))
    run = lambda *a: subprocess.run([sys.executable, str(scripts / "box.py"), "--site", str(path), *a],
                                    capture_output=True, text=True, timeout=30)
    r = run("site", "check")
    assert r.returncode == 1 and "demo missing" in r.stdout
    site["boxes"]["<box-alias>"]["demo"] = True
    path.write_text(json.dumps(site))
    r = run("site", "check")
    assert r.returncode == 0 and "warning" not in r.stdout, r.stdout
    assert run("demo").stdout.strip() == "demo ran"

    spec = importlib.util.spec_from_file_location("box_under_test", BOX_PY)
    box = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(box)
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(box.subprocess, "run", fake_run)
    box.ssh_run("<box-alias>", ["echo", "a b"])
    assert seen["argv"][:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    assert seen["argv"][-2:] == ["<box-alias>", "echo 'a b'"]


def test_setup_box_links_tt_buddy_instead_of_copying():
    agents = (SKILLS / "setup-box" / "agents.md").read_text(encoding="utf-8")
    for ref in ("recipes/developer-setup.md", "recipes/workspace.md", "recipes/tt-metal/build.md",
                "recipes/tt-metal/env.md", "recipes/tt-metal/test.md", "tt-buddy:run", "skills/run/mcp.md"):
        assert ref in agents, ref
    for page in ("ird", "slurm", "exabox", "bare-metal", "migrate-off-sync"):
        assert (SKILLS / "setup-box" / f"{page}.md").is_file(), page


def test_setup_box_states_company_etiquette_without_personal_policy():
    # Company-wide practice only; a user's own limits belong in their charter (user msg #554).
    skill = (SKILLS / "setup-box" / "SKILL.md").read_text(encoding="utf-8")
    for part in ("allocated, drained, reserved or down", "release right after it",
                 "Never\n  auto-extend", "hold nothing idle", "charter can narrow these rules"):
        assert part in skill, part
    assert "IRD is reachable only from networks that can reach the IRD service" in \
        (SKILLS / "setup-box" / "ird.md").read_text(encoding="utf-8")
    pages = "\n".join(p.read_text(encoding="utf-8") for p in (SKILLS / "setup-box").rglob("*")
                      if p.is_file() and p.suffix in (".md", ".py")).lower()
    for banned in ("idle at least", "nodes the user listed", "nodes you listed", "machine list",
                   "reservation cap", "reservation-cap"):
        assert banned not in pages, banned


def _bare_env(tmp_path, *, ssh_down=False, broker=True, svc="active", free_kb=500 * 1024 ** 2,
              machines=None, lock_rc=0):
    """PATH shims: ssh runs the remote command in a local shell over fake df/systemctl/broker; ttp
    answers `machines list --json` and `lock --probe`. Every ssh and ttp call is logged."""
    import shlex
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "calls.log"

    def shim(name, body):
        p = bin_ / name
        p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        p.chmod(0o755)
    shim("ssh", f'echo "ssh $*" >> {log}\n' + ("echo 'connection refused' >&2; exit 255\n" if ssh_down
                                              else 'exec bash -c "${@: -1}"\n'))
    shim("df", f'echo "Filesystem 1024-blocks Used Available Capacity Mounted"\n'
               f'echo "/dev/x 999999999 1 {free_kb} 1% /"\n')
    shim("systemctl", f'[ "$2" = tt-device-broker ] && echo {svc} || echo inactive\n'
                      f'[ "{svc}" = active ]\n')
    if broker:
        shim("tt-device-mcp", "exit 0\n")
    shim("ttp", f'echo "ttp $*" >> {log}\n'
                f'[ "$1" = machines ] && {{ echo {shlex.quote(json.dumps(machines or {}))}; exit 0; }}\n'
                f'exit {lock_rc}\n')
    env = {k: v for k, v in os.environ.items() if k != "TTP_BOXES_FILE"}
    env["PATH"] = f"{bin_}:/usr/bin:/bin"
    return env, log


def _bare_check(tmp_path, env, *args, site=None):
    import subprocess
    path = tmp_path / "boxes.json"
    path.write_text(json.dumps(site or {"version": 1, "boxes": {"<box-alias>": {
        "route": "bare", "workdir_root": "~/<workdir>"}}}), encoding="utf-8")
    return subprocess.run([sys.executable, str(BOX_PY), "--site", str(path), "bare", "check", "<box-alias>", *args],
                          capture_output=True, text=True, timeout=60, env=env)


def test_setup_box_bare_check_ready_box(tmp_path):
    env, log = _bare_env(tmp_path, machines={"<box-alias>": {"tags": ["device"], "min_free_gb": 100}})
    r = _bare_check(tmp_path, env)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "ssh: ok" in out and "500 GB free" in out and "guard 100 GB" in out
    assert "tt-device-mcp present, tt-device-broker active" in out
    assert "lock <box-alias>-device: free" in out and out.rstrip().endswith("verdict: ready")
    calls = log.read_text()
    # One non-interactive ssh call; the device lock is only probed, never taken.
    assert calls.count("ssh ") == 1 and "BatchMode=yes" in calls
    assert "ttp lock --probe <box-alias>-device" in calls
    facts = json.loads(_bare_check(tmp_path, env, "--json").stdout)
    assert facts["rc"] == 0 and facts["broker_service"] == "active" and facts["tags"] == ["device"]


@pytest.mark.parametrize("kw, args, rc, expect", [
    (dict(free_kb=10 * 1024 ** 2), (), 1, "below the guard of 150 GB"),
    (dict(free_kb=10 * 1024 ** 2), ("--min-free-gb", "0"), 0, "verdict: ready"),
    (dict(machines={"<box-alias>": {"tags": [], "min_free_gb": 5}}, free_kb=10 * 1024 ** 2), (), 0, "guard 5 GB"),
    (dict(svc="inactive"), (), 1, "do not start a second broker"),
    (dict(broker=False, svc="inactive"), (), 0, "broker: none"),
    (dict(lock_rc=75), (), 75, "busy or paused: wait for it, never override it"),
    (dict(ssh_down=True, lock_rc=75), (), 255, "ssh: failed (exit 255) connection refused"),
    (dict(machines={"<box-alias>": {"tags": ["device", "serving", "unstable"]}}), (), 0, "use it last"),
    (dict(machines={}), (), 0, "not in `ttp machines list`"),
])
def test_setup_box_bare_check_problems(tmp_path, kw, args, rc, expect):
    env, _ = _bare_env(tmp_path, **kw)
    r = _bare_check(tmp_path, env, *args)
    assert r.returncode == rc and expect in r.stdout, r.stdout + r.stderr


def test_setup_box_bare_check_custom_lock_and_wrong_route(tmp_path):
    env, log = _bare_env(tmp_path)
    site = {"version": 1, "boxes": {"<box-alias>": {"route": "slurm", "workdir_root": "/<workdir>",
                                                     "slurm": {"login": "<l>", "partitions": []}}}}
    r = _bare_check(tmp_path, env, "--resource", "<res>", site=site)
    assert "route is 'slurm', not bare" in r.stdout and "lock <res>: free" in r.stdout
    assert "ttp lock --probe <res>" in log.read_text()


def test_setup_box_bare_metal_page_covers_the_route():
    page = (SKILLS / "setup-box" / "bare-metal.md").read_text(encoding="utf-8")
    for part in ("ttp machines list", "ttp lock --probe <box-alias>-device", "--until", "serving",
                 "unstable", "tt-buddy:run", "skills/run/mcp.md", "Never start a second broker",
                 "TT_VISIBLE_DEVICES", "--min-free-gb", "recipes/developer-setup.md",
                 "git submodule update --init --recursive", "ccache", "not portable",
                 "box.py bare check <box-alias>"):
        assert part in page, part


def _migrate_home(tmp_path, present):
    """A fake HOME (plus PATH stubs for mutagen and crontab), with every sync leftover or none."""
    home, stubs, repo = tmp_path / "home", tmp_path / "stubs", tmp_path / "proj"
    for d in (home / ".ssh", stubs, repo, home / ".claude" / "skills"):
        d.mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host <box-alias>\n    User <user>\n")
    mutagen = "echo 'unable to connect to daemon' >&2; exit 1"
    crontab = "echo 'no crontab for user' >&2; exit 1"
    if present:
        ttw = home / "tt-workflows"
        for d in ("skills/tt-old", "hooks", "scripts"):
            (ttw / d).mkdir(parents=True)
        (ttw / "scripts" / "devrun.sh").write_text("#!/bin/sh\n")
        (ttw / "hooks" / "guard.py").write_text("")
        units = home / ".config" / "systemd" / "user"
        units.mkdir(parents=True)
        (units / "devsync.service").write_text("[Service]\n")
        (units / "default.target.wants").mkdir()
        (units / "default.target.wants" / "devsync.service").symlink_to(units / "devsync.service")
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        (home / "Library" / "LaunchAgents" / "org.example.devsync.plist").write_text("")
        with (home / ".ssh" / "config").open("a") as f:
            f.write("\n# devsync-managed ssh-hosts\nInclude ~/.devsync/ssh-hosts\n")
        for skills in (".claude/skills", ".agents/skills", ".cursor/skills"):
            (home / skills).mkdir(parents=True, exist_ok=True)
            (home / skills / "tt-old").symlink_to(ttw / "skills" / "tt-old")
        (home / ".claude" / "skills" / "tt-gone").symlink_to(ttw / "skills" / "tt-gone")
        (home / ".claude" / "skills" / "tt-mine").symlink_to(tmp_path)
        hook = f"python3 {ttw}/hooks/guard.py"
        (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": hook}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/true"}]}]}}))
        (home / ".codex").mkdir()
        (home / ".codex" / "hooks.json").write_text(json.dumps(
            {"hooks": {"PreToolUse": [{"hooks": [{"command": f"{ttw}/hooks/missing.sh"}]}]}}))
        (home / ".cursor" / "hooks.json").write_text(json.dumps(
            {"version": 1, "hooks": {"beforeShellExecution": [{"command": hook}]}}))
        mutagen = ("printf 'Name: proj-sync\\nIdentifier: sync_abc\\nAlpha:\\n\\tURL: /src\\n'; "
                   '[ "$MUTAGEN_DISABLE_AUTOSTART" = 1 ] || echo autostarted')
        crontab = (f"echo '*/5 * * * * {ttw}/scripts/cron_lap.sh --dir x'; "
                   f"echo '# 0 * * * * {ttw}/scripts/old.sh'; echo '0 1 * * * /usr/bin/true'")
        (repo / "ttw.toml").write_text('[host]\ndevice_host = "<box-alias>"\nremote_root = "/<workdir>"\n')
        (repo / "run.sh").write_text("#!/bin/sh\n~/tt-workflows/scripts/devrun.sh make\n")
        (repo / "run.log").write_text("devrun.sh quoted in a log\n")
    systemctl = "exit 3"  # is-active: inactive
    for name, body in (("mutagen", mutagen), ("crontab", crontab), ("systemctl", systemctl)):
        (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
        (stubs / name).chmod(0o755)
    return home, stubs, repo


def _migrate_run(home, stubs, repo, *extra):
    import subprocess
    env = dict(os.environ, HOME=str(home), PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}")
    return subprocess.run([sys.executable, str(BOX_PY), "migrate", "check", "--home", str(home),
                           "--repo", str(repo), *extra], capture_output=True, text=True, timeout=60, env=env)


def _tree(root):
    return sorted((str(p), p.is_symlink() or p.stat().st_mtime_ns) for p in root.rglob("*"))


def test_setup_box_migrate_check_reports_every_leftover_and_changes_nothing(tmp_path):
    home, stubs, repo = _migrate_home(tmp_path, present=True)
    before = _tree(tmp_path)
    r = _migrate_run(home, stubs, repo)
    assert r.returncode == 1, r.stdout + r.stderr
    out = r.stdout
    assert "found devsync-unit: " in out and "devsync.service" in out and "org.example.devsync.plist" in out
    assert "found ssh-include: " in out and "Include ~/.devsync/ssh-hosts" in out
    assert "found mutagen: sync session proj-sync (sync_abc)" in out and "autostarted" not in out
    for skills in (".claude/skills", ".agents/skills", ".cursor/skills"):
        assert f"found skill-link: {home / skills / 'tt-old'} -> " in out, skills
    assert "inactive skill-link: " in out and "tt-gone" in out and "(dangling)" in out
    assert "tt-mine" not in out
    assert "found hook: " + str(home / ".claude/settings.json") + " PreToolUse: python3 " in out
    assert "/usr/bin/true" not in out
    assert str(home / ".codex/hooks.json") + " PreToolUse: " in out and "script missing" in out
    assert str(home / ".cursor/hooks.json") + " beforeShellExecution: " in out
    assert "found cron: crontab line 1:" in out and "inactive cron: crontab line 2:" in out
    assert "crontab line 3" not in out
    assert 'device_host = "<box-alias>"' in out and 'remote_root = "/<workdir>"' in out
    assert f"found devrun-caller: {repo / 'run.sh'}" in out and "run.log" not in out
    assert "report only: nothing was changed" in out
    assert _tree(tmp_path) == before
    data = json.loads(_migrate_run(home, stubs, repo, "--json").stdout)
    kinds = {f["kind"] for f in data["found"] if f["active"]}
    assert kinds == {"devsync-unit", "ssh-include", "mutagen", "skill-link", "hook", "cron",
                     "ttw-toml", "devrun-caller"}


def test_setup_box_migrate_check_is_clear_when_nothing_is_left(tmp_path):
    home, stubs, repo = _migrate_home(tmp_path, present=False)
    r = _migrate_run(home, stubs, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    for kind in ("devsync-unit", "ssh-include", "mutagen", "skill-link", "hook", "cron", "ttw-toml",
                 "devrun-caller"):
        assert f"clear {kind}\n" in r.stdout, kind
    assert "found" not in r.stdout and "0 active leftover(s)" in r.stdout


def test_setup_box_migrate_check_clears_a_devsync_unit_the_guide_disabled(tmp_path):
    home, stubs, repo = _migrate_home(tmp_path, present=False)
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "devsync.service").write_text("[Service]\n")  # after `systemctl --user disable --now`
    r = _migrate_run(home, stubs, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"inactive devsync-unit: {units / 'devsync.service'} (disabled and stopped)" in r.stdout
    (stubs / "systemctl").write_text('#!/bin/sh\n[ "$*" = "--user is-active --quiet devsync.service" ]\n')
    r = _migrate_run(home, stubs, repo)
    assert r.returncode == 1 and "found devsync-unit: " in r.stdout and "(running)" in r.stdout


def test_setup_box_migrate_check_skips_tt_projects_own_files(tmp_path):
    home, stubs, repo = _migrate_home(tmp_path, present=False)
    import shutil
    import subprocess
    own = [("plugins/tt-project/skills/setup-box/scripts/box_migrate.py", BOX_PY.parent / "box_migrate.py"),
           ("plugins/tt-project/skills/setup-box/migrate-off-sync.md",
            SKILLS / "setup-box" / "migrate-off-sync.md"),
           ("tests/test_tt_project_plugin.py", pathlib.Path(__file__))]
    for rel, src in own:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, repo / rel)
    (repo / "run.sh").write_text("#!/bin/sh\n~/tt-workflows/scripts/devrun.sh make\n")
    (repo / "tools").mkdir()
    (repo / "tools" / "go.py").write_text('import subprocess\nsubprocess.run(["devlock.sh", "x"])\n')
    for git_repo in (False, True):
        if git_repo:
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        r = _migrate_run(home, stubs, repo)
        assert r.returncode == 1, r.stdout + r.stderr
        callers = sorted(l for l in r.stdout.splitlines() if "devrun-caller" in l)
        assert callers == [f"found devrun-caller: {repo / 'run.sh'}",
                           f"found devrun-caller: {repo / 'tools' / 'go.py'}"], (git_repo, callers)


def test_setup_box_migration_guide_orders_steps_and_keeps_defaults_on():
    text = (SKILLS / "setup-box" / "migrate-off-sync.md").read_text(encoding="utf-8")
    steps = ["devsync is-finished", "mutagen sync terminate", "git worktree", "tt-buddy:run",
             "managed\n     include", "after its replacement is in place", "skill symlinks",
             "Archive tt-workflows"]
    pos = [text.find(s) for s in steps]
    assert -1 not in pos and pos == sorted(pos), dict(zip(steps, pos))
    assert "box.py migrate check" in text and "ttp note --to <project>" in text
    assert "Never edit another project's harness" in text
    assert "nothing to turn on" in text and "released\n   right after" in text


def test_readme_documents_the_product_defaults():
    """Caps, the plan line and its reserve, free Codex resets, light-tier models and Jev as an add-on."""
    text = (PLUGIN / "README.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "$100 per 24 hours and $200 per 7 days per project by default" in flat
    assert "Global daily cap (usage-billed, $200/day by default)" in flat
    assert "The 90% line is the default (`budget.reserve_pct` 10)" in flat
    assert "only that 10% is left for you" in flat and "raise the reserve" in flat
    assert "Free Codex window resets" in flat and "used automatically, at most one per window" in flat
    tiers = text.split("## Model tiers", 1)[1].split("\n## ", 1)[0]
    assert "`sonnet`" in tiers and "A task's own model or effort pin wins over its tier" in " ".join(tiers.split())
    jev = " ".join(text.split("## Jev (optional add-on)", 1)[1].split("\n## ", 1)[0].split())
    assert "optional and off until you save a key" in jev
    assert "sends log and alert text" in jev and "outside paid service" in jev


def _fake_slurm(tmp_path, node="State=IDLE LastBusyTime=2000-01-01T00:00:00", res="", queue=""):
    """A bin dir whose ssh runs the remote command locally, with fake Slurm tools reading files."""
    fake = tmp_path / "fake"
    bin_ = tmp_path / "bin"
    fake.mkdir()
    bin_.mkdir()
    (fake / "node.txt").write_text(f"NodeName=node07 Arch=x86_64 CPUTot=1 {node} Reason=a State=IDLE b\n")
    (fake / "res.txt").write_text(res)
    (fake / "squeue.txt").write_text(queue)
    (fake / "sinfo.txt").write_text("node07\n")
    tools = {
        "ssh": 'while [ "${1#-}" != "$1" ]; do shift 2; done; shift; exec bash -c "$*"',
        "scontrol": 'case "$*" in *reservation*) [ -s "$FAKE/res.txt" ] && cat "$FAKE/res.txt" || '
                    '{ echo "No reservations in the system"; exit 1; };; *) cat "$FAKE/node.txt";; esac',
        "sinfo": 'cat "$FAKE/sinfo.txt"',
        "squeue": 'cat "$FAKE/squeue.txt"',
        "sbatch": 'echo "$*" > "$FAKE/sbatch.args"; for a; do s=$a; done; cp "$s" "$FAKE/job.sbatch"; echo 4242',
    }
    for name, body in tools.items():
        (bin_ / name).write_text(f"#!/bin/bash\n{body}\n")
        (bin_ / name).chmod(0o755)
    site = _site_example()
    site["boxes"]["<box-alias>"]["workdir_root"] = str(tmp_path / "work")
    site["boxes"]["<box-alias>"]["slurm"]["partitions"][0]["name"] = "p4"
    site["boxes"]["<box-alias>"]["demo_nodes"] = ["demo1"]
    site["boxes"]["<box-alias>"]["excluded_nodes"] = ["bad1"]
    (tmp_path / "boxes.json").write_text(json.dumps(site))
    env = {k: v for k, v in os.environ.items() if k != "TTP_BOXES_FILE"}
    env.update(PATH=f"{bin_}:{env['PATH']}", FAKE=str(fake))

    def run(*a):
        import subprocess
        return subprocess.run([sys.executable, str(BOX_PY), "--site", str(tmp_path / "boxes.json"), "slurm", *a],
                              capture_output=True, text=True, timeout=60, env=env)
    return run, fake


def test_setup_box_slurm_check_takes_only_an_idle_unreserved_node(tmp_path):
    import datetime
    run, fake = _fake_slurm(tmp_path)
    r = run("check", "<box-alias>", "node07", "--partition", "p4")
    assert r.returncode == 0 and r.stdout.startswith("ok "), r.stdout + r.stderr
    assert run("check", "<box-alias>", "node07", "--allow", "n1,n2").stdout.startswith("refused: node07 is not in the allowed")
    assert "demo_nodes" in run("check", "<box-alias>", "demo1").stdout
    assert "excluded_nodes" in run("check", "<box-alias>", "bad1").stdout
    for state in ("ALLOCATED", "MIXED", "IDLE+DRAIN", "DOWN+NOT_RESPONDING", "IDLE+RESERVED", "IDLE+MAINT",
                  "IDLE*", "IDLE~", "IDLE$", "IDLE@", "IDLE#", "IDLE%", "IDLE!", "IDLE^", "IDLE-"):
        (fake / "node.txt").write_text(f"NodeName=node07 State={state} LastBusyTime=2000-01-01T00:00:00\n")
        r = run("check", "<box-alias>", "node07")
        assert r.returncode == 1 and f"state is {state}" in r.stdout, (state, r.stdout)
    (fake / "node.txt").write_text("NodeName=node07 State=IDLE LastBusyTime=2000-01-01T00:00:00\n")
    (fake / "res.txt").write_text("ReservationName=maint1 Nodes=a[01-03],node07 State=ACTIVE\n")
    r = run("check", "<box-alias>", "node07")
    assert r.returncode == 1 and "reservation maint1" in r.stdout
    (fake / "res.txt").write_text("")
    recent = (datetime.datetime.now() - datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    (fake / "node.txt").write_text(f"NodeName=node07 State=IDLE LastBusyTime={recent}\n")
    assert run("check", "<box-alias>", "node07").returncode == 0  # no idle minimum unless the charter sets one
    r = run("check", "<box-alias>", "node07", "--min-idle", "120")
    assert r.returncode == 1 and "LastBusyTime" in r.stdout
    assert run("check", "<box-alias>", "node07", "--min-idle", "20").returncode == 0
    (fake / "sinfo.txt").write_text("")
    assert "not in partition p4" in run("check", "<box-alias>", "node07", "--partition", "p4").stdout


def test_setup_box_slurm_hostlist_expansion():
    sys.path.insert(0, str(BOX_PY.parent))
    try:
        import box_slurm
    finally:
        sys.path.remove(str(BOX_PY.parent))
    assert box_slurm.expand_hostlist("n[01-03,07],m5,x[9-10]y") == ["n01", "n02", "n03", "n07", "m5", "x9y", "x10y"]


def test_setup_box_slurm_submit_refuses_unsafe_jobs_and_prints_note_and_probe(tmp_path):
    run, fake = _fake_slurm(tmp_path)
    co = str(tmp_path / "work" / "repo")
    base = ["submit", "<box-alias>", "--node", "node07", "--partition", "p4", "--name", "t1",
            "--checkout", co, "--purpose", "smoke test"]
    r = run(*base, "--command", "make test")
    assert r.returncode == 1 and "--time is required" in r.stdout
    assert "not a Slurm time limit" in run(*base, "--time", "UNLIMITED", "--command", "true").stdout
    for cmd in ("salloc -N1", "sbatch --no-shell x; make"):
        assert "salloc and --no-shell are not allowed" in run(*base, "--time", "30", "--command", cmd).stdout
    r = run(*base, "--time", "30", "--command", "echo 'unbalanced")
    assert r.returncode == 1 and "cannot be parsed" in r.stdout and "Traceback" not in r.stderr
    assert "not under workdir_root" in run(*base[:-4], "--checkout", f"{tmp_path}/work/../x", "--purpose", "p",
                                           "--time", "30", "--command", "true").stdout
    assert "not under workdir_root" in run(*base[:-4], "--checkout", "/tmp/x", "--purpose", "p",
                                           "--time", "30", "--command", "true").stdout
    assert "partition 'nope'" in run(*[("nope" if a == "p4" else a) for a in base], "--time", "30", "--command", "true").stdout
    (fake / "node.txt").write_text("NodeName=node07 State=ALLOCATED\n")
    assert "state is ALLOCATED" in run(*base, "--time", "30", "--command", "true").stdout
    assert not (fake / "sbatch.args").exists()

    (fake / "node.txt").write_text("NodeName=node07 State=IDLE\n")
    r = run(*base, "--time", "1:10:00", "--devices", "0", "--command", "make test")
    assert r.returncode == 0, r.stdout + r.stderr
    args = (fake / "sbatch.args").read_text()
    assert "--parsable --exclusive -w node07 -p p4 --time=1:10:00" in args
    script = (fake / "job.sbatch").read_text()
    assert f"cd {co}" in script and "export TT_VISIBLE_DEVICES=0" in script and "trap cleanup EXIT" in script
    assert (tmp_path / "work" / "runs" / "t1" / "job.sbatch").is_file()
    note = next(ln for ln in r.stdout.splitlines() if ln.startswith("note: ttp note "))
    for part in ("cluster <box-alias>", "job 4242", "node node07", "start ", "purpose: smoke test"):
        assert part in note, part
    probe = next(ln for ln in r.stdout.splitlines() if ln.startswith("retry_when: "))[len("retry_when: "):]
    import subprocess
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "FAKE": str(fake)}
    (fake / "squeue.txt").write_text("4242\n")
    assert subprocess.run(probe, shell=True, env=env).returncode == 1
    (fake / "squeue.txt").write_text("17\n")
    assert subprocess.run(probe, shell=True, env=env).returncode == 0


def test_setup_box_slurm_status_and_verify_clean(tmp_path):
    run, fake = _fake_slurm(tmp_path, queue="4242|RUNNING|node07|5:00|1:10:00|node07\n")
    r = run("status", "<box-alias>", "4242")
    assert r.returncode == 0 and "job 4242 RUNNING node node07" in r.stdout
    (fake / "squeue.txt").write_text("")
    rundir = tmp_path / "work" / "runs" / "t1"
    rundir.mkdir(parents=True)
    (rundir / "job.log").write_text("line1\nall passed\n")
    r = run("status", "<box-alias>", "4242", "--name", "t1")
    assert "no longer in squeue" in r.stdout and "rc missing" in r.stdout and "all passed" in r.stdout
    (rundir / "job.rc").write_text("0\n")
    assert "rc 0" in run("status", "<box-alias>", "4242", "--name", "t1").stdout
    (rundir / "job.rc").unlink()
    squeue = tmp_path / "bin" / "squeue"
    squeue.write_text('#!/bin/bash\necho "squeue: error: Unable to contact slurm controller" >&2; exit 1\n')
    r = run("status", "<box-alias>", "4242", "--name", "t1")  # unknown, not ended: no resubmit cue
    assert r.returncode == 2 and "squeue failed" in r.stdout, r.stdout
    assert "ended" not in r.stdout and "rc missing" not in r.stdout
    squeue.write_text('#!/bin/bash\necho "slurm_load_jobs error: Invalid job id specified" >&2; exit 1\n')
    r = run("status", "<box-alias>", "4242", "--name", "t1")
    assert r.returncode == 0 and "no longer in squeue" in r.stdout and "rc missing" in r.stdout
    squeue.write_text('#!/bin/bash\ncat "$FAKE/squeue.txt"\n')
    r = run("verify-clean", "<box-alias>", "node07")
    assert r.returncode == 0 and "clean: no job" in r.stdout and "cannot check processes" in r.stdout
    (fake / "squeue.txt").write_text("4242 RUNNING\n")
    r = run("verify-clean", "<box-alias>", "node07")
    assert r.returncode == 1 and "not clean" in r.stdout


def _run_batch_script(tmp_path, command, signal_after=None):
    import signal
    import subprocess
    import time
    sys.path.insert(0, str(BOX_PY.parent))
    try:
        import box_slurm
    finally:
        sys.path.remove(str(BOX_PY.parent))
    co = tmp_path / "co"
    co.mkdir()
    rc = tmp_path / "job.rc"
    script = tmp_path / "job.sbatch"
    script.write_text(box_slurm.batch_script(str(co), command, str(rc), None))
    env = {k: v for k, v in os.environ.items() if k != "TTP_JOB_PGRP"}
    p = subprocess.Popen(["bash", str(script)], cwd=tmp_path, env=env)  # not a group leader: re-execs via setsid
    if signal_after is not None:
        time.sleep(signal_after)
        p.send_signal(signal.SIGTERM)
    p.wait(timeout=30)
    pid = int((co / "child.pid").read_text())
    for _ in range(50):
        if not os.path.exists(f"/proc/{pid}") or "Z" in open(f"/proc/{pid}/stat").read().split()[2]:
            break
        time.sleep(0.1)
    alive = os.path.exists(f"/proc/{pid}") and "Z" not in open(f"/proc/{pid}/stat").read().split()[2]
    return rc.read_text().strip(), alive


def test_setup_box_slurm_batch_script_kills_its_process_group(tmp_path):
    rc, alive = _run_batch_script(tmp_path, "sleep 300 & echo $! > child.pid; exit 3")
    assert rc == "3" and not alive


def test_setup_box_slurm_batch_script_cleans_up_on_term(tmp_path):
    rc, alive = _run_batch_script(tmp_path, "sleep 300 & echo $! > child.pid; wait", signal_after=1.5)
    assert rc == "143" and not alive


def test_setup_box_exabox_agrees_with_worker_shared_clusters():
    worker = (PLUGIN / "template/prompts/worker.md").read_text(encoding="utf-8")
    shared = worker[worker.index("## Shared clusters"):worker.index("<!-- /ttp:if clusters -->")]
    pages = {name: (SKILLS / "setup-box" / name).read_text(encoding="utf-8")
             for name in ("exabox.md", "slurm.md")}
    for text in (shared, *pages.values()):
        flat = " ".join(text.split())
        assert "sbatch" in flat
        assert re.search(r"(Never|No interactive or no-shell allocation \() ?`salloc --no-shell`", flat)
        assert "allocated, drained, reserved" in flat and "ttp note" in flat
    for name, text in pages.items():
        assert "--min-idle" in text, name  # a charter's narrowing, never a default


# --- setup-box IRD route: box.py ird against a fake `ird` behind a fake ssh ----------------------

FAKE_SSH = r'''#!/bin/sh
echo "$*" >> "$FAKE_DIR/ssh.log"
if [ "$1" = "-G" ]; then printf 'hostname %s\nport %s\n' "$FAKE_SSH_HOST" "$FAKE_SSH_PORT"; exit 0; fi
while [ $# -gt 1 ]; do case "$1" in -o) shift 2;; -*) shift;; *) shift; break;; esac; done
exec sh -c "$1"
'''

FAKE_IRD = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
path = os.path.join(d, "ird.json")
st = json.load(open(path))
a = sys.argv[1:]
open(os.path.join(d, "ird.log"), "a").write(" ".join(a) + "\n")
cmd = a[0]
if cmd == "list-machines":
    print("MACHINE POOL ARCH MODEL STATE REASON")
    for m in st["machines"]:
        print(" ".join(m))
elif cmd == "list":
    print("2026-10-14 08:00:00 INFO connected")
    for r in st["reservations"]:
        print(f"{r['id']} {r['user']} pool part {r['machine']} a b c d e {r['port']}")
elif cmd == "reserve":
    flags, rest, i = {}, a[1:], 0
    while i < len(rest) and rest[i].startswith("--"):
        if rest[i] == "--no-shell":
            flags["no-shell"] = True; i += 1
        else:
            flags[rest[i][2:]] = rest[i + 1]; i += 2
    if len(rest) - i != 1 or os.environ.get("FAKE_IRD_NOOP"):
        print("usage: ird reserve [options] <arch>")  # a flag after the arch: silently no reservation
        sys.exit(0)
    rid = str(st["next_id"]); st["next_id"] += 1
    machine = flags.get("machine") or next(m[0] for m in st["machines"] if m[4] == "idle")
    st["reservations"].append({"id": rid, "user": os.environ["FAKE_IRD_USER"], "machine": machine,
                               "port": flags.get("ssh-port", str(40000 + int(rid)))})
elif cmd in ("release", "change-timeout", "reboot"):
    r = [r for r in st["reservations"] if r["id"] == a[1]]
    if not r:
        print("no such reservation"); sys.exit(1)
    if cmd == "release":
        st["reservations"].remove(r[0])
    if cmd == "reboot":
        for m in st["machines"]:
            if m[0] == r[0]["machine"]:
                m[4:] = ["draining@", "Reboot", "ASAP"]
json.dump(st, open(path, "w"))
'''


class FakeIrd:
    def __init__(self, tmp_path, port):
        import stat
        self.dir = tmp_path / "fake"
        bin_ = self.dir / "bin"
        bin_.mkdir(parents=True)
        for name, body in (("ssh", FAKE_SSH), ("ird", FAKE_IRD)):
            (bin_ / name).write_text(body)
            (bin_ / name).chmod(0o755)
        self.state = {"next_id": 100, "reservations": [], "machines": [
            ["m-idle", "p", "arch", "x", "idle"], ["m-mixed", "p", "arch", "x", "mixed"],
            ["m-alloc", "p", "arch", "x", "alloc"], ["m-drain", "p", "arch", "x", "draining"],
            ["m-down", "p", "arch", "x", "down*"], ["m-fail", "p", "arch", "x", "failed"],
            ["m-asap", "p", "arch", "x", "idle", "Reboot", "ASAP"], ["m-excl", "p", "arch", "x", "idle"]]}
        self.save()
        self.site = tmp_path / "boxes.json"
        self.site.write_text(json.dumps({"version": 1, "boxes": {"<ird-box>": {
            "route": "ird", "workdir_root": "/<workdir>", "excluded_nodes": ["m-excl"],
            "ird": {"mgmt": "<ird-mgmt-alias>", "pools": ["<pool>", "<pool2>"], "user": "me"}}}}))
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("TTP_")}
        self.env.update(PATH=f"{bin_}:{os.environ['PATH']}", HOME=str(self.home), FAKE_DIR=str(self.dir),
                        FAKE_SSH_HOST="127.0.0.1", FAKE_SSH_PORT=str(port), FAKE_IRD_USER="me",
                        TTP_IRD_DIR=str(tmp_path / "ird"), TTP_PROJECT="proj", TTP_TASK="t1")
        self.ird_dir = tmp_path / "ird"

    def save(self):
        (self.dir / "ird.json").write_text(json.dumps(self.state))

    def load(self):
        self.state = json.loads((self.dir / "ird.json").read_text())
        return self.state

    def ird_log(self):
        p = self.dir / "ird.log"
        return p.read_text().splitlines() if p.exists() else []

    def run(self, *args, **env):
        import subprocess
        r = subprocess.run([sys.executable, str(BOX_PY), "--site", str(self.site), "ird", *args],
                           capture_output=True, text=True, timeout=60, env={**self.env, **env})
        return r.returncode, r.stdout + r.stderr

    def add(self, rid, user="me", machine="m-mixed", port="4242"):
        self.load()["reservations"].append({"id": rid, "user": user, "machine": machine, "port": port})
        self.save()

    def claim(self, rid, **kw):
        c = {"id": rid, "project": "proj", "task": "t0", "host": "elsewhere", "pid": 1, "heartbeat": 0}
        c.update(kw)
        p = self.ird_dir / "claims" / f"<ird-box>.{rid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(c))


@pytest.fixture
def fake_ird(tmp_path):
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    yield FakeIrd(tmp_path, srv.getsockname()[1])
    srv.close()


def test_setup_box_ird_list_skips_draining_down_and_reboot_pending(fake_ird):
    rc, out = fake_ird.run("list", "<ird-box>")
    assert rc == 0, out
    assert out.split("\n")[:2] == ["free m-idle idle", "free m-mixed mixed"] and out.count("free") == 2
    rc, out = fake_ird.run("list", "<ird-box>", "--all")
    for line in ("skip m-alloc busy (alloc)", "skip m-drain state draining", "skip m-down state down*",
                 "skip m-fail state failed", "skip m-asap reboot pending", "skip m-excl excluded"):
        assert line in out, out
    assert all(l.startswith("list-machines") for l in fake_ird.ird_log())
    assert "-o BatchMode=yes -o ConnectTimeout=15 -n <ird-mgmt-alias> ird list-machines" in \
        (fake_ird.dir / "ssh.log").read_text()


def test_setup_box_ird_reserve_puts_every_flag_before_the_arch(fake_ird):
    rc, out = fake_ird.run("reserve", "<ird-box>", "--arch", "<arch>", "--timeout", "02:30:00",
                           "--machine", "m-idle", "--ssh-port", "4100")
    assert rc == 0, out
    assert re.search(r"^reserved 100 machine=m-idle port=4100 expires=\S+", out, re.M), out
    reserve = [l for l in fake_ird.ird_log() if l.startswith("reserve")]
    assert reserve == ["reserve --cluster <pool> --timeout 02:30:00 --machine m-idle --ssh-port 4100 --no-shell <arch>"]
    claim = json.loads((fake_ird.ird_dir / "claims" / "<ird-box>.100.json").read_text())
    assert (claim["project"], claim["task"], claim["machine"], claim["port"]) == ("proj", "t1", "m-idle", "4100")
    assert claim["pid"] > 0 and claim["heartbeat"] > 0
    hosts = (fake_ird.ird_dir / "ssh-hosts").read_text()
    assert "Host m-idle\n    HostName m-idle\n    Port 4100\n    ProxyJump <ird-mgmt-alias>\n" in hosts
    assert "StrictHostKeyChecking" not in hosts
    assert "Include" in out  # ~/.ssh/config lacks the include: say so, never edit it
    assert not (fake_ird.home / ".ssh" / "config").exists()


@pytest.mark.parametrize("args, message", [
    (("--timeout", "max"), "--timeout must be hh:mm:ss"),
    (("--timeout", "01:00:00", "--pool", "<other>"), "not in ird.pools"),
    (("--timeout", "01:00:00", "--machine", "m-drain"), "refused: m-drain: state draining"),
    (("--timeout", "01:00:00", "--machine", "m-asap"), "refused: m-asap: reboot pending"),
    (("--timeout", "01:00:00", "--machine", "m-excl"), "refused: m-excl: excluded"),
])
def test_setup_box_ird_reserve_refuses_bad_requests(fake_ird, args, message):
    rc, out = fake_ird.run("reserve", "<ird-box>", "--arch", "<arch>", *args)
    assert rc == 1 and message in out, out
    assert not [l for l in fake_ird.ird_log() if l.startswith("reserve")]


def test_setup_box_ird_reserve_fails_when_ird_silently_reserves_nothing(fake_ird):
    fake_ird.add("7", user="other")
    rc, out = fake_ird.run("reserve", "<ird-box>", "--arch", "<arch>", "--timeout", "01:00:00", FAKE_IRD_NOOP="1")
    assert rc == 1 and "made no reservation" in out, out
    assert not (fake_ird.ird_dir / "claims").exists()


def test_setup_box_ird_acts_only_on_the_callers_reservations(fake_ird):
    fake_ird.add("5", user="other", machine="m-alloc")
    fake_ird.claim("5")
    fake_ird.add("6")
    fake_ird.claim("6", task="t1")
    rc, out = fake_ird.run("status", "<ird-box>")
    assert rc == 0 and "6 m-mixed port=4242" in out and "m-alloc" not in out, out
    for verb in (("release", "5"), ("extend", "5", "30"), ("reboot", "5")):
        rc, out = fake_ird.run(verb[0], "<ird-box>", *verb[1:])
        assert rc == 1 and "belongs to other" in out, out
    rc, out = fake_ird.run("release", "<ird-box>", "--stale")
    assert rc == 0 and "released 6" in out and "5" not in out, out
    assert [r["id"] for r in fake_ird.load()["reservations"]] == ["5"]
    assert not any(l.startswith(("release 5", "change-timeout", "reboot")) for l in fake_ird.ird_log())


def test_setup_box_ird_release_follows_claims(fake_ird):
    import time
    rc, out = fake_ird.run("reserve", "<ird-box>", "--arch", "<arch>", "--timeout", "01:00:00")
    assert rc == 0, out
    fake_ird.add("8")  # no claim: made by hand or elsewhere
    fake_ird.add("9", machine="m-idle")
    fake_ird.claim("9", project="other-proj")
    rc, out = fake_ird.run("release", "<ird-box>", "100", TTP_TASK="t2")
    assert rc == 1 and "live claim of task 't1'" in out, out
    rc, out = fake_ird.run("release", "<ird-box>", "8")
    assert rc == 1 and "no claim" in out, out
    rc, out = fake_ird.run("release", "<ird-box>", "9")
    assert rc == 1 and "claimed by project 'other-proj'" in out, out
    rc, out = fake_ird.run("release", "<ird-box>", "--stale", TTP_TASK="t2")
    assert rc == 0, out
    assert "keep 100" in out and "skip 8" in out and "skip 9" in out, out
    assert {r["id"] for r in fake_ird.load()["reservations"]} == {"100", "8", "9"}
    rc, out = fake_ird.run("release", "<ird-box>", "100")  # the task that made it
    assert rc == 0 and "released 100" in out, out
    assert not (fake_ird.ird_dir / "claims" / "<ird-box>.100.json").exists()
    assert "Host" not in (fake_ird.ird_dir / "ssh-hosts").read_text()
    fake_ird.claim("10", heartbeat=time.time())  # its reservation already ended
    fake_ird.claim("11", project="other-proj")
    rc, out = fake_ird.run("release", "<ird-box>", "--stale")
    assert (fake_ird.ird_dir / "claims" / "<ird-box>.11.json").exists()
    assert not (fake_ird.ird_dir / "claims" / "<ird-box>.10.json").exists()


def test_setup_box_ird_extend_on_demand_and_reboot_then_re_reserve(fake_ird):
    rc, out = fake_ird.run("reserve", "<ird-box>", "--arch", "<arch>", "--timeout", "01:00:00",
                           "--machine", "m-mixed", "--ssh-port", "4300")
    assert rc == 0, out
    rc, out = fake_ird.run("extend", "<ird-box>", "100", "45")
    assert rc == 0 and "extended 100 by 45 min" in out, out
    rc, out = fake_ird.run("reboot", "<ird-box>", "100")
    assert rc == 0, out
    assert [l for l in fake_ird.ird_log() if l.startswith(("reboot", "release"))] == ["reboot 100", "release 100"]
    assert "--machine m-mixed" in out and "--ssh-port 4300" in out and "--arch '<arch>'" in out, out
    assert not fake_ird.load()["reservations"]
    rc, out = fake_ird.run("list", "<ird-box>")
    assert "m-mixed" not in out  # draining until it reboots


def test_setup_box_ird_says_so_when_ird_is_unreachable(fake_ird, tmp_path):
    import socket
    rc, out = fake_ird.run("status", "<ird-box>", FAKE_SSH_HOST="ird-mgmt.invalid")
    assert rc == 255 and "no DNS name" in out and "reachable only from networks" in out, out
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed = s.getsockname()[1]
    s.close()
    rc, out = fake_ird.run("list", "<ird-box>", FAKE_SSH_PORT=str(closed))
    assert rc == 255 and "no route to its ssh port" in out, out
    assert fake_ird.ird_log() == []
