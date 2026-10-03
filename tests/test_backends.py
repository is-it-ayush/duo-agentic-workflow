"""Implementor backend: ollama | claude (a headless Claude Code session). Fixed by server.py at start.
The `claude` CLI is replaced by tests/fake_claude.py, which runs the real server tool code."""
import json, os, pathlib, subprocess, sys, types
import pytest
from helpers import FakeChat, Rpc, call, draft_plan, reply, step_text, tool_pass

FAKE = pathlib.Path(__file__).with_name("fake_claude.py")


def git(srv, *a):
    return subprocess.run(["git", *a], cwd=srv.ROOT, capture_output=True, text=True, check=True).stdout.strip()


def act(tool, **args):
    return {"tool": tool, "args": args}


PASS = [act("write_file", path="ok.txt", content="x"), act("finish_step")]


@pytest.fixture
def cc(load_server, tmp_path_factory, monkeypatch):
    """A claude-backend server whose `claude` is the fake CLI. cc.play(*entries) scripts its invocations."""
    bindir = tmp_path_factory.mktemp("bin")
    exe, script = bindir / "claude", bindir / "script.json"
    exe.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
    exe.chmod(0o755)
    for k, v in {"AGENT_IMPLEMENTOR": "claude", "AGENT_MODEL": "haiku", "AGENT_CLAUDE_BIN": str(exe),
                 "FAKE_CLAUDE_SCRIPT": str(script)}.items():
        monkeypatch.setenv(k, v)
    srv = load_server()
    calls_file = script.with_suffix(".calls")

    def play(*entries):
        script.write_text(json.dumps(list(entries)))

    def calls():
        return [json.loads(l) for l in calls_file.read_text().splitlines()] if calls_file.exists() else []
    return types.SimpleNamespace(srv=srv, play=play, calls=calls)


def plan(srv, cmd="test -f ok.txt", lock=""):
    out = draft_plan(srv, [step_text(cmd, lock=lock)])
    assert "phase=BUFFER" in out, out


# ---------- selection ----------
def test_one_model_setting_serves_both_backends(load_server, monkeypatch):
    srv = load_server()
    assert srv.IMPLEMENTOR == "ollama" and srv.MODEL == "qwen3:8b"
    assert not hasattr(srv, "CLAUDE_MODEL") and f"implementor=ollama:{srv.MODEL}" in srv.fsm_status()
    monkeypatch.setenv("AGENT_MODEL", "llama3.1:8b")
    assert load_server().MODEL == "llama3.1:8b"


def test_claude_label_uses_the_same_agent_model(cc):
    assert cc.srv.impl_label() == "claude:haiku" and "implementor=claude:haiku" in cc.srv.fsm_status()


def test_unknown_backend_is_refused_without_side_effects(load_server, monkeypatch):
    monkeypatch.setenv("AGENT_IMPLEMENTOR", "bogus")
    srv = load_server()
    plan(srv)
    before = srv.load()
    out = srv.run_implementor()
    assert out.startswith("refused") and "AGENT_IMPLEMENTOR" in out
    assert srv.load() == before and "UNUSABLE" in srv.fsm_status()
    with pytest.raises(ValueError):
        srv.chat([{"role": "user", "content": "x"}])


def test_claude_backend_needs_an_explicit_model_and_the_cli(load_server, monkeypatch, tmp_path_factory):
    monkeypatch.setenv("AGENT_IMPLEMENTOR", "claude")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    srv = load_server()
    plan(srv)
    before = srv.load()
    out = srv.run_implementor()
    assert out.startswith("refused") and "AGENT_MODEL" in out and srv.load() == before
    monkeypatch.setenv("AGENT_MODEL", "haiku")
    monkeypatch.setenv("AGENT_CLAUDE_BIN", "/nonexistent/claude")
    srv2 = load_server(git=False)
    out = srv2.run_implementor()
    assert out.startswith("refused") and "claude CLI not found" in out and "UNUSABLE" in srv2.fsm_status()


def test_chat_is_the_ollama_path_only(cc):
    with pytest.raises(ValueError, match="Ollama"):
        cc.srv.chat([{"role": "user", "content": "x"}])


def test_ollama_selected_never_launches_claude(srv, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("claude launched while IMPLEMENTOR=ollama")
    monkeypatch.setattr(srv, "launch_claude", boom)
    plan(srv)
    monkeypatch.setattr(srv, "chat", FakeChat(*tool_pass()))
    assert "phase=VALIDATE" in srv.run_implementor()


def test_claude_selected_never_uses_ollama(cc, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ollama used while IMPLEMENTOR=claude")
    monkeypatch.setattr(cc.srv, "chat", boom)
    monkeypatch.setattr(cc.srv.ollama, "chat", boom)
    plan(cc.srv)
    cc.play({"actions": PASS})
    assert "phase=VALIDATE" in cc.srv.run_implementor()


def test_no_mcp_tool_can_choose_the_backend_or_model(srv):
    import inspect
    for fn in (srv.fsm_status, srv.fsm_to, srv.run_implementor):
        params = list(inspect.signature(fn).parameters)
        assert not [p for p in params if any(k in p.lower() for k in ("backend", "model", "implementor"))], fn


# ---------- a headless Claude Code session ----------
def test_session_is_locked_to_our_tools_and_the_configured_model(cc):
    srv = cc.srv
    plan(srv)
    cc.play({"actions": PASS})
    assert "phase=VALIDATE" in srv.run_implementor()
    (c,) = cc.calls()
    a = c["argv"]
    val = lambda flag: a[a.index(flag) + 1]
    assert "-p" in a and val("--model") == "haiku" and val("--output-format") == "stream-json" and "--verbose" in a
    assert "--strict-mcp-config" in a and val("--tools") == ""
    assert val("--allowedTools").split(",") == [f"mcp__impl__{t}" for t in srv.IMPL_TOOLS]
    assert val("--max-turns") == str(srv.MAX_TOOL_CALLS)
    assert "Style (director and implementor)" in val("--append-system-prompt")     # same prompt files as Qwen
    cfg = json.loads(val("--mcp-config"))["mcpServers"]
    assert list(cfg) == ["impl"] and cfg["impl"]["args"][-1] == "--impl-tools"
    assert cfg["impl"]["env"]["AGENT_PROJECT"] == str(srv.ROOT)
    assert c["env"] == {"AGENT_IMPL_RUN": "1", "AGENT_PROJECT": str(srv.ROOT)} and c["cwd"] == str(srv.ROOT)
    assert c["stdin"].startswith("STEP 1/1\n")                                      # the task goes in on stdin


def test_full_step_commits_records_progress_and_handoff(cc):
    srv = cc.srv
    plan(srv)
    cc.play({"actions": PASS})
    out = srv.run_implementor()
    assert "phase=VALIDATE" in out and "implementor=claude:haiku" in out and "commits=1:" in out
    assert srv.load()["implementor"] == "claude:haiku"
    assert "implementor: claude:haiku" in git(srv, "log", "-1", "--format=%b")
    assert "step 1: g | files: ok.txt" in srv.PROGRESS.read_text()
    assert (srv.HANDOFF / "implement.md").read_text().startswith("RUN 1\nimplementor: claude:haiku")
    log = (srv.AG / "run.log").read_text()
    assert "[session] sess-fake" in log and "claude --resume sess-fake" in log      # to inspect the session afterwards
    assert "[call] mcp__impl__write_file(" in log and "[result] ok" in log          # the live view


def test_every_attempt_is_a_new_process_told_what_the_last_one_did(cc):
    srv = cc.srv
    plan(srv)
    cc.play({"actions": [act("write_file", path="bad.txt", content="x")], "final": "I think it is done"},
            {"actions": PASS})
    assert "phase=VALIDATE" in srv.run_implementor()
    first, second = cc.calls()
    assert "PREVIOUS ATTEMPT" not in first["stdin"]
    assert "PREVIOUS ATTEMPT FAILED (1/4)" in second["stdin"] and "I think it is done" in second["stdin"]
    assert "exit=1" in second["stdin"] and srv.load()["attempts"] == 0


def test_failing_finish_step_calls_inside_one_session_escalate(cc):
    srv = cc.srv
    plan(srv, cmd="false")
    cc.play({"actions": [act("finish_step")] * (srv.MAX_ATTEMPTS + 1), "final": "gave up"})
    out = srv.run_implementor()
    assert "phase=BUFFER" in out and "step=1" in out and len(cc.calls()) == 1
    assert (srv.ATTEMPTS / "01.log").read_text().count("--- attempt") == srv.MAX_ATTEMPTS + 1
    assert "gave up" in srv.CHECKPOINT.read_text()
    s = srv.load()
    assert (s["attempts"], s["escalations"], s["needs_directive"]) == (0, 1, True)
    assert "escalated at step 1" in (srv.HANDOFF / "implement.md").read_text()


def test_stopping_without_finish_step_is_an_attempt(cc):
    srv = cc.srv
    plan(srv, cmd="false")
    cc.play({"actions": [], "final": "done"})                       # every process just stops
    assert "phase=BUFFER" in srv.run_implementor()
    assert len(cc.calls()) == srv.MAX_ATTEMPTS + 1                  # a fresh process per attempt
    assert srv.load()["escalations"] == 1


def test_blocked_escalates_immediately_with_the_reason(cc):
    srv = cc.srv
    plan(srv, cmd="false")
    cc.play({"actions": [act("blocked", reason="spec contradicts code")], "final": "stopping"})
    assert "phase=BUFFER" in srv.run_implementor() and len(cc.calls()) == 1
    assert "BLOCKED: spec contradicts code" in srv.CHECKPOINT.read_text()


def test_a_finished_step_refuses_further_tool_use(srv):
    srv.AG.mkdir(exist_ok=True)
    srv.RESULT_FILE.write_text('{"status": "pass"}')
    with pytest.raises(ValueError, match="already finished"):
        srv.impl_guard()


def test_locked_files_are_enforced_inside_the_session(cc):
    srv = cc.srv
    (srv.ROOT / "locked.py").write_text("ORIG")
    plan(srv, lock="locked.py")
    cc.play({"actions": [act("write_file", path="locked.py", content="weakened"), act("finish_step"),
                         act("write_file", path="locked.py", content="ORIG")] + PASS})
    assert "phase=VALIDATE" in srv.run_implementor()
    assert "LOCKED file modified" in (srv.ATTEMPTS.parent / "run.log").read_text()


def test_timeouts_and_max_turns_are_ordinary_failed_attempts(cc, monkeypatch):
    srv = cc.srv
    monkeypatch.setattr(srv, "CLAUDE_ATTEMPT_TIMEOUT", 1)
    plan(srv)
    cc.play({"actions": [], "sleep": 30},                                                   # hangs after "finishing"
            {"actions": [], "error": {"subtype": "error_max_turns", "text": "too many turns"}},
            {"actions": PASS})
    assert "phase=VALIDATE" in srv.run_implementor() and len(cc.calls()) == 3


@pytest.mark.parametrize("entry,expected", [
    ({"tools": ["Bash", "mcp__impl__run"], "delay_after_init": 1.5}, "unexpected tools"),
    ({"extra_servers": [{"name": "agent", "status": "connected"}], "delay_after_init": 1.5}, "unexpected MCP servers"),
    ({"model": "claude-sonnet-4-5", "delay_after_init": 1.5}, "model check failed"),
    ({"impl_status": "failed"}, "impl MCP server failed"),
    ({"no_init": True, "delay_before_tool": 1.5}, "no init event"),
    ({"actions": [], "error": {"subtype": "error_during_execution", "text": "Invalid API key. Please run /login"}},
     "claude error"),
], ids=["builtin-tool", "director-server", "wrong-model", "tool-server-down", "no-init", "auth-error"])
def test_unsafe_or_broken_sessions_are_refused_before_any_work(cc, entry, expected):
    srv = cc.srv
    plan(srv)
    cc.play({"actions": [act("write_file", path="bad.txt", content="x")] + PASS, **entry})
    out = srv.run_implementor()
    assert "ERROR=" in out and expected in out, out
    s = srv.load()
    assert (s["phase"], s["attempts"], s["needs_directive"], srv.RUNNING) == ("BUFFER", 0, False, False)
    assert not (srv.ROOT / "bad.txt").exists() and not (srv.ROOT / "ok.txt").exists()
    assert subprocess.run(["git", "log"], cwd=srv.ROOT, capture_output=True).returncode != 0    # nothing committed


# ---------- the MCP tool server the session talks to ----------
def test_impl_tool_server_exposes_only_the_sandboxed_tools(srv, tmp_path):
    plan(srv)
    srv.CTX_FILE.write_text(json.dumps({"n": 1, "attempts": 0, "locks0": {}}))
    env = {**os.environ, "AGENT_PROJECT": str(srv.ROOT)}
    rpc = Rpc([sys.executable, str(srv.HOME / "server.py"), "--impl-tools"], env, srv.ROOT)
    try:
        rpc.handshake()
        tools = {t["name"]: t for t in rpc.call(2, "tools/list")["result"]["tools"]}
        assert list(tools) == list(srv.IMPL_TOOLS) and not [n for n in tools if n.startswith("fsm") or "implementor" in n]
        import inspect
        for name, fn in srv.make_tools().items():
            assert set(tools[name]["inputSchema"].get("properties", {})) == set(inspect.signature(fn).parameters), name

        def text(r):
            return r["result"]["content"][0]["text"]
        assert text(rpc.call(3, "tools/call", {"name": "write_file", "arguments": {"path": "ok.txt", "content": "x"}})) == "ok"
        assert "outside" in text(rpc.call(4, "tools/call", {"name": "delete_path", "arguments": {"path": "../x"}})) or \
            rpc.call(5, "tools/call", {"name": "read_file", "arguments": {"path": "../x"}})["result"]["isError"]
        assert text(rpc.call(6, "tools/call", {"name": "finish_step", "arguments": {}})).startswith("PASS")
        after = rpc.call(7, "tools/call", {"name": "write_file", "arguments": {"path": "more.txt", "content": "y"}})
        assert after["result"]["isError"] and "already finished" in text(after)
        assert not (srv.ROOT / "more.txt").exists()
    finally:
        rpc.close()


def test_second_step_gets_the_previous_steps_summary_in_its_new_session(cc):
    srv = cc.srv
    steps = [step_text(f"test -f a{i}.txt", goal=f"goal {i}") for i in (1, 2)]
    assert "phase=BUFFER" in draft_plan(srv, steps)
    cc.play({"actions": [act("write_file", path="a1.txt", content="x"), act("finish_step")]},
            {"actions": [act("write_file", path="a2.txt", content="x"), act("finish_step")]})
    assert "phase=VALIDATE" in srv.run_implementor()
    first, second = cc.calls()
    assert "PREVIOUS STEPS" not in first["stdin"]
    assert "PREVIOUS STEPS (already done, do not redo):\n- step 1: goal 1 | files: a1.txt" in second["stdin"]
    assert second["stdin"].startswith("STEP 2/2")
    assert (srv.ROOT / "a2.txt").exists()
    assert git(srv, "show", "--name-only", "--format=", "HEAD") == "a2.txt"


def test_one_steps_verdict_never_leaks_into_the_next_step(cc):
    """Mutation-testing found this gap: a leftover 'pass' result would let step 2 pass without doing any work."""
    srv = cc.srv
    steps = [step_text(f"test -f a{i}.txt", goal=f"goal {i}") for i in (1, 2)]
    assert "phase=BUFFER" in draft_plan(srv, steps)
    cc.play({"actions": [act("write_file", path="a1.txt", content="x"), act("finish_step")]},
            {"actions": [], "final": "done"})                                  # step 2's sessions do nothing
    out = srv.run_implementor()
    assert "phase=BUFFER" in out and "step=2" in out, out
    assert not (srv.ROOT / "a2.txt").exists() and srv.load()["pointer"] == 2
    assert "commits=1:" in out and "2:" not in out.split("commits=")[1].split(" ")[0]
    assert not srv.RESULT_FILE.exists()
