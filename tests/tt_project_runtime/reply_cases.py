# What a task that answers a chat posts there (runtime/ttp/replymsg.py, Daemon._finish_worker, pushq._reply).
# Run from test_tt_project_runtime.py.

# Task #3304's hand-off: the steps it was asked to send were only in its tool output.
_SELF_DESCRIBED = ("Extracted the MAC RERUN steps from run 7021 result.json and returned them as the message for "
                   "the user. No code, branch or state changed.")


def _reply_hand_off(env, p, result: dict, chat="c1", kind="work"):
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task("send the steps", "spec", kind=kind, tier="light", origin="coordinator")
    p.db.update_task(tid, status="running", reply_chat=chat)
    run_dir = env["tmp"] / f"run-reply-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(result))
    d = Daemon(p.base)
    with d.p.db.tx():
        d._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    replies = [m["text"] for m in p.db.q("SELECT text FROM messages WHERE kind='reply' AND chat=?", (chat,))]
    return p.db.task(tid), replies


def test_a_reply_posts_the_hand_offs_message_not_its_summary(env):
    from ttp.db import load_result
    p = make(env)
    steps = "1. Run `ttp notifier reset`.\n2. Run `ttp notifier check` and paste back what it prints."
    task, replies = _reply_hand_off(env, p, {"status": "done", "summary": "Sent the user the rerun steps.",
                                             "message": steps})
    assert task["status"] == "done" and replies == [steps]
    assert load_result(task["result"])["message"] == steps, "the record keeps what was sent"


def test_a_reply_without_a_deliverable_is_not_posted_and_goes_to_review(env):
    from ttp.db import load_result
    p = make(env)
    for result in ({"status": "done", "summary": _SELF_DESCRIBED},
                   {"status": "done", "summary": "Done.", "message": "The steps are written out above, ready to send."},
                   {"status": "done", "summary": ""}):
        task, replies = _reply_hand_off(env, p, result)
        assert replies == [], result
        assert task["status"] == "review" and "nothing was posted to the chat" in task["blocked_reason"], result
        assert load_result(task["result"])["undelivered"] in task["blocked_reason"]
        [ev] = p.db.q("SELECT * FROM events WHERE task=? AND kind='task_review'", (task["id"],))
        assert ev["status"] == "queued" and "Not posted to the chat" in ev["text"], "the coordinator sees it"


def test_ordinary_answers_still_post(env):
    p = make(env)
    for summary in ("Yes: the daemon restarts after a reboot (daemon.py:3791).",
                    "The notifier sent the user a toast at 09:30 PDT; its log shows it was delivered.",
                    "The user's config sets review.auto to false, so no review was queued.",
                    "Run it again with --strict.",
                    "Sent the fix to the push queue; it landed as 1a2b3c4.",
                    "Coverage is 92%; it is above the 90% target.",
                    "Yes, the release note is ready to send.",
                    "Sent the user the steps: 1) git pull 2) ttp setup 3) ttp notifier test."):
        task, replies = _reply_hand_off(env, p, {"status": "done", "summary": summary}, chat=f"c{len(summary)}")
        assert task["status"] == "done" and replies == [summary], summary
    # A failed or blocked run describing itself is still reported: its summary says what went wrong.
    task, _ = _reply_hand_off(env, p, {"status": "blocked", "summary": _SELF_DESCRIBED, "question": "which box?"},
                              chat="cb")
    assert task["status"] == "blocked"


def test_replymsg_flags_only_short_self_descriptions():
    from ttp import replymsg
    for t in (_SELF_DESCRIBED, "Returned the Mac rerun steps to the chat.", "Relayed them to the user.",
              "Gave the user the steps.", "The message is above.", "Drafted it; ready to send."):
        assert replymsg.undelivered({"summary": t}), t
    long_steps = _SELF_DESCRIBED + "\nSteps:\n1. `git checkout abc`\n2. `ttp notifier check`"
    for t in (long_steps, "You returned them as the message for the user; that is fine.\n" * 12,
              "Yes, it is.", "Sent you the steps by mail.", "As shown in the log, the job passed.",
              # a comparison, not the reply placed above
              "Coverage is 92%; it is above the 90% target.", "Temperature: it is above 80C on chip 3.",
              "Spend this week: they are above budget on both boxes.", "The answer is above 1000 for every run.",
              # a named deliverable's state, not the reply's
              "Yes, the release note is ready to send.",
              # the deliverable follows the phrase
              "Sent the user the steps: 1) git pull 2) ttp setup 3) ttp notifier test.",
              "Mac rerun steps (ready to paste): git pull; ttp setup; ttp notifier test.",
              "Returned them as the message for the user: run ttp setup, then ttp notifier test."):
        assert not replymsg.undelivered({"summary": t}), t
    for t in ("The steps are written out above.", "The steps are written out above, ready to send.",
              "Done: returned them as the message for the user."):
        assert replymsg.undelivered({"summary": t}), t
    assert replymsg.text({"summary": "s", "message": "  "}) == "s", "an empty message falls back to the summary"
    assert replymsg.text({"summary": "s", "message": "m"}) == "m"


def test_a_landed_reply_posts_its_message_with_how_it_landed():
    from ttp import pushq
    assert pushq._done_text({"message": "Here are the steps."}, "s (landed)", "landed") == "Here are the steps. (landed)"
    assert pushq._done_text({"summary": "s"}, "s (landed)", "landed") == "s (landed)"


def test_a_reply_chat_worker_prompt_says_the_message_is_relayed_verbatim(env):
    from ttp.prompts import worker_task
    p = make(env)
    tid = p.db.add_task("send the steps", "spec", kind="work", tier="light", origin="coordinator")
    assert "## Reply to the chat" not in worker_task(p, p.db.task(tid), str(p.root), None)
    p.db.update_task(tid, reply_chat="c1")
    text = worker_task(p, p.db.task(tid), str(p.root), None)
    assert "## Reply to the chat" in text and "`message` there verbatim" in text and "deliverable" in text
