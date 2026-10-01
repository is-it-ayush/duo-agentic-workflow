"""Implementor loop with a scripted fake Qwen. Covers attempts, escalation, directives, halting, locks, sandbox."""
import os
import pytest
from helpers import FakeChat, call, draft_plan, reply, step_text, tool_pass

PROMPT_BUDGET_CHARS = 4000     # style.md + implementor.md; tune to taste (~4 chars/token)


def build(srv, n=1, cmd=None, mode="", pointer=0, lock=""):
    steps = [step_text(cmd or f"test -f a{i}.txt", lock=lock) for i in range(1, n + 1)]
    out = draft_plan(srv, steps, mode=mode, pointer=pointer)
    assert "phase=BUFFER" in out, out


def use(monkeypatch, srv, fake):
    monkeypatch.setattr(srv, "chat", fake)
    return fake


def attempts_log(srv, n=1):
    return (srv.ATTEMPTS / f"{n:02d}.log").read_text()


# ---------- happy paths ----------
def test_progressive_runs_every_step_in_a_fresh_context(srv, monkeypatch):
    build(srv, 3)
    fake = use(monkeypatch, srv, FakeChat(*[r for i in (1, 2, 3) for r in tool_pass(f"a{i}.txt")]))
    assert "phase=VALIDATE" in srv.run_implementor()
    s = srv.load()
    assert (s["phase"], s["pointer"], s["attempts"], s["escalations"]) == ("VALIDATE", 4, 0, 0)
    assert [m.splitlines()[0] for m in fake.first_user_messages()] == ["STEP 1/3", "STEP 2/3", "STEP 3/3"]
    assert len(fake.calls) == 6


def test_direct_mode_runs_only_the_pointed_step(srv, monkeypatch):
    build(srv, 3, mode="direct", pointer=2)
    fake = use(monkeypatch, srv, FakeChat(*tool_pass("a2.txt")))
    assert "phase=VALIDATE" in srv.run_implementor()
    s = srv.load()
    assert (s["pointer"], s["mode"]) == (2, "direct")
    assert fake.first_user_messages()[0].startswith("STEP 2/3") and len(fake.calls) == 2
    assert not (srv.ROOT / "a1.txt").exists() and not (srv.ROOT / "a3.txt").exists()


def test_server_runs_the_tests_not_the_model(srv, monkeypatch):
    build(srv, 1, cmd="test -f ok.txt")
    fake = use(monkeypatch, srv, FakeChat(reply(call("finish_step")), *tool_pass("ok.txt")))
    assert "phase=VALIDATE" in srv.run_implementor()          # first finish_step claimed done but wasn't
    assert fake.calls[1][-1]["content"].startswith("FAIL attempt 1/")


# ---------- failure handling ----------
def test_escalates_on_the_attempt_after_the_limit(srv, monkeypatch):
    build(srv, 1, cmd="false")
    fake = use(monkeypatch, srv, FakeChat(default=reply(call("finish_step"), content="PROBLEM: p")))
    out = srv.run_implementor()
    assert "phase=BUFFER" in out and "step=1" in out
    assert len(fake.calls) == srv.MAX_ATTEMPTS + 2            # (MAX+1) finish calls + 1 checkpoint call
    s = srv.load()
    assert (s["phase"], s["attempts"], s["escalations"], s["needs_directive"]) == ("BUFFER", 0, 1, True)
    assert srv.CHECKPOINT.read_text().startswith("STEP 1") and "PROBLEM: p" in srv.CHECKPOINT.read_text()
    assert attempts_log(srv).count("--- attempt") == srv.MAX_ATTEMPTS + 1


def test_blocked_escalates_immediately(srv, monkeypatch):
    build(srv, 1, cmd="false")
    fake = use(monkeypatch, srv, FakeChat(reply(call("blocked", reason="spec contradicts code")),
                                         default=reply(content="PROBLEM: b")))
    assert "phase=BUFFER" in srv.run_implementor()
    assert len(fake.calls) == 2                               # blocked + checkpoint
    assert "BLOCKED: spec contradicts code" in attempts_log(srv)
    assert srv.load()["escalations"] == 1


def test_tool_spam_counts_as_failed_attempts(srv, monkeypatch):
    monkeypatch.setattr(srv, "MAX_TOOL_CALLS", 3)
    build(srv, 1, cmd="false")
    fake = use(monkeypatch, srv, FakeChat(default=reply(call("read_file", path="nope.txt"), content="still going")))
    assert "phase=BUFFER" in srv.run_implementor()
    assert len(fake.calls) == (srv.MAX_ATTEMPTS + 1) * 3 + 1
    log = attempts_log(srv)
    assert "tool-call budget exhausted" in log and "still going" in log     # last reply is recorded


def test_text_only_reply_counts_as_finish_step_and_can_pass(srv, monkeypatch):
    """Regression (live run): Qwen did the work, then answered in prose and never called finish_step."""
    build(srv, 1, cmd="test -f ok.txt")
    fake = use(monkeypatch, srv, FakeChat(reply(call("write_file", path="ok.txt", content="x")),
                                         reply(content="Done.")))
    assert "phase=VALIDATE" in srv.run_implementor()
    assert len(fake.calls) == 2


def test_text_only_reply_that_fails_is_an_attempt_not_a_loop(srv, monkeypatch):
    build(srv, 1, cmd="false")
    fake = use(monkeypatch, srv, FakeChat(default=reply(content="Done.")))
    assert "phase=BUFFER" in srv.run_implementor()
    assert len(fake.calls) == srv.MAX_ATTEMPTS + 2                  # MAX+1 verified replies + checkpoint
    assert fake.calls[1][-1]["content"].startswith("FAIL attempt 1/")   # model is told what failed


def test_chat_failure_hands_control_back_and_is_retryable(srv, monkeypatch):
    build(srv, 1, cmd="test -f ok.txt")

    def boom(*a, **k):
        raise ConnectionError("ollama down")
    monkeypatch.setattr(srv, "chat", boom)
    out = srv.run_implementor()
    assert "ERROR=" in out and "ollama down" in out
    s = srv.load()
    assert (s["phase"], s["needs_directive"], srv.RUNNING) == ("BUFFER", False, False)
    use(monkeypatch, srv, FakeChat(*tool_pass("ok.txt")))
    assert "phase=VALIDATE" in srv.run_implementor()


# ---------- directive / halt ----------
def test_directive_is_required_injected_then_cleared(srv, monkeypatch):
    build(srv, 1, cmd="test -f ok.txt")
    use(monkeypatch, srv, FakeChat(reply(call("blocked", reason="r")), default=reply(content="PROBLEM: b")))
    srv.run_implementor()
    assert srv.run_implementor().startswith("refused")        # needs_directive, none written
    srv.DIRECTIVE.write_text("CAUSE: x\nDO: 1. create ok.txt")
    fake = use(monkeypatch, srv, FakeChat(*tool_pass("ok.txt")))
    assert "phase=VALIDATE" in srv.run_implementor()
    assert "DIRECTIVE:\nCAUSE: x" in fake.first_user_messages()[0]
    assert not srv.DIRECTIVE.exists() and srv.load()["escalations"] == 0


def test_halts_after_too_many_escalations_until_user_guided(srv, monkeypatch):
    build(srv, 1, cmd="test -f ok.txt")
    use(monkeypatch, srv, FakeChat(default=reply(call("blocked", reason="r"))))
    for i in range(srv.MAX_ESCALATIONS + 1):
        if i:
            srv.DIRECTIVE.write_text("DO: try again")
        assert "phase=BUFFER" in srv.run_implementor()
    s = srv.load()
    assert s["halted"] is True and s["escalations"] == srv.MAX_ESCALATIONS + 1
    srv.DIRECTIVE.write_text("DO: user says X")
    assert "escalation limit" in srv.run_implementor()
    use(monkeypatch, srv, FakeChat(*tool_pass("ok.txt")))
    assert "phase=VALIDATE" in srv.run_implementor(user_guided=True)
    assert srv.load()["halted"] is False


# ---------- locks / verification ----------
def test_locked_files_cannot_be_modified(srv, monkeypatch):
    (srv.ROOT / "locked.py").write_text("ORIG")
    build(srv, 1, cmd="test -f ok.txt", lock="locked.py")
    fake = use(monkeypatch, srv, FakeChat(
        reply(call("write_file", path="locked.py", content="weakened")), reply(call("finish_step")),
        reply(call("write_file", path="locked.py", content="ORIG")),
        reply(call("write_file", path="ok.txt", content="x")), reply(call("finish_step"))))
    assert "phase=VALIDATE" in srv.run_implementor()
    assert "LOCKED" in fake.calls[2][-1]["content"]


def test_verify_stops_at_first_failing_command(srv):
    ok, out = srv.verify(step_text("true\nfalse\ntouch nope.txt"), {})
    assert not ok and "exit=1" in out and not (srv.ROOT / "nope.txt").exists()


def test_verify_treats_missing_binary_as_failure(srv):
    ok, out = srv.verify(step_text("no-such-binary-xyz"), {})
    assert not ok and "exit=1" in out


# ---------- implementor sandbox + prompt scope ----------
def test_tool_sandbox(srv, tmp_path_factory):
    t = srv.make_tools()
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("s")
    os.symlink(outside / "secret.txt", srv.ROOT / "link")
    srv.PLAN.mkdir(parents=True, exist_ok=True)
    (srv.PLAN / "01.md").write_text("plan")
    assert t["read_file"](".agent/plan/01.md") == "plan"                    # reading the plan is fine
    for bad in ("../outside.txt", "/etc/passwd", "link"):
        with pytest.raises(ValueError, match="escapes"):
            t["read_file"](bad)
    with pytest.raises(ValueError, match="escapes"):
        t["write_file"]("link", "pwned")
    assert (outside / "secret.txt").read_text() == "s"
    for p in (".agent/state.json", ".agent/plan/01.md"):
        with pytest.raises(ValueError, match="read-only"):
            t["write_file"](p, "x")
    assert t["write_file"]("sub/dir/f.txt", "a") == "ok" and (srv.ROOT / "sub/dir/f.txt").read_text() == "a"
    assert t["run"]("rm -rf /tmp/zzz").startswith("denied")
    assert t["run"]("ls").startswith("exit=0")
    assert "python3" in srv.ALLOW, "Debian has python3, not python; add it to ALLOW"


def test_implementor_prompt_is_scoped_and_small(srv):
    (srv.ROOT / "AGENTS.md").write_text("PROJECT-FACT-123\n" + "x" * 5000)
    style = (srv.HOME / "common/style.md").read_text()
    impl = (srv.HOME / "implementor/implementor.md").read_text()
    p = srv.system_prompt()
    assert style in p and impl in p and "PROJECT-FACT-123" in p
    assert "x" * 3001 not in p                                              # AGENTS.md is truncated
    for rel in ("common/protocol.md", "common/discussion.md", "director/director.md"):
        first = next(l for l in (srv.HOME / rel).read_text().splitlines() if l.strip())
        assert first not in p, f"{rel} leaked into the implementor prompt"
    assert len(style) + len(impl) <= PROMPT_BUDGET_CHARS, "Qwen's fixed prompt grew; trim style.md/implementor.md"
