"""run.log shows what the model is doing live: thinking, text, tool calls. Uses a fake ollama.chat (no server)."""
import types
import ollama
from helpers import draft_plan, step_text


def chunk(content="", thinking=None, calls=None):
    return types.SimpleNamespace(message=types.SimpleNamespace(
        content=content, thinking=thinking, tool_calls=calls))


def tc(name, **args):
    return ollama.Message.ToolCall(function=ollama.Message.ToolCall.Function(name=name, arguments=args))


class FakeOllama:
    """Replaces ollama.chat. Each scripted entry is the list of chunks for one model call."""
    def __init__(self, *calls):
        self.script, self.kwargs = list(calls), []

    def __call__(self, **kw):
        self.kwargs.append(kw)
        chunks = self.script.pop(0)
        if kw.get("stream"):
            return iter(chunks)
        msg = types.SimpleNamespace(
            content="".join(c.message.content or "" for c in chunks),
            thinking="".join(c.message.thinking or "" for c in chunks) or None,
            tool_calls=[t for c in chunks for t in (c.message.tool_calls or [])] or None)
        return types.SimpleNamespace(message=msg)


def runlog(srv):
    return (srv.AG / "run.log").read_text()


def test_chat_streams_thinking_text_and_calls_to_run_log(srv, monkeypatch):
    fake = FakeOllama([chunk(thinking="let me "), chunk(thinking="check"), chunk(content="Creating "),
                       chunk(content="file"), chunk(calls=[tc("write_file", path="a.txt", content="x")])])
    monkeypatch.setattr(srv.ollama, "chat", fake)
    srv.CTX["label"] = "step 7"
    resp = srv.chat([{"role": "user", "content": "hi"}], tools=[])
    assert resp.message.content == "Creating file"
    assert resp.message.thinking is None, "thinking must be logged, not fed back into the history"
    assert resp.message.tool_calls[0].function.name == "write_file"
    log = runlog(srv)
    for expected in ("step 7", "[think] let me check", "[say] Creating file",
                     '[call] write_file({"path": "a.txt", "content": "x"})'):
        assert expected in log, expected
    kw = fake.kwargs[0]
    assert kw["stream"] is True and kw["think"] is False and kw["options"]["temperature"] == 0


def test_thinking_is_off_by_default_and_on_with_the_env_flag(load_server, monkeypatch):
    srv = load_server()
    assert srv.THINK is False
    monkeypatch.setenv("AGENT_THINK", "1")
    srv = load_server(git=False)
    fake = FakeOllama([chunk(content="ok")])
    monkeypatch.setattr(srv.ollama, "chat", fake)
    srv.chat([{"role": "user", "content": "hi"}])
    assert srv.THINK is True and fake.kwargs[0]["think"] is True


def test_non_streaming_fallback_logs_the_same_things(load_server, monkeypatch):
    monkeypatch.setenv("AGENT_STREAM", "0")
    srv = load_server()
    fake = FakeOllama([chunk(thinking="hmm"), chunk(content="done"), chunk(calls=[tc("finish_step")])])
    monkeypatch.setattr(srv.ollama, "chat", fake)
    resp = srv.chat([{"role": "user", "content": "hi"}])
    assert "stream" not in fake.kwargs[0]
    assert resp.message.content == "done" and resp.message.tool_calls[0].function.name == "finish_step"
    assert "[think] hmm" in runlog(srv) and "[say] done" in runlog(srv) and "[call] finish_step({})" in runlog(srv)


def test_full_step_through_the_streaming_chat_keeps_thinking_out_of_history(srv, monkeypatch):
    assert "phase=BUFFER" in draft_plan(srv, [step_text("test -f ok.txt")])
    fake = FakeOllama(
        [chunk(thinking="plan it"), chunk(content="writing"),
         chunk(calls=[tc("write_file", path="ok.txt", content="x")])],
        [chunk(calls=[tc("finish_step")])])
    monkeypatch.setattr(srv.ollama, "chat", fake)
    assert "phase=VALIDATE" in srv.run_implementor()
    log = runlog(srv)
    assert "step 1" in log and "[think] plan it" in log and "[call] finish_step({})" in log
    assistants = [m for m in fake.kwargs[0]["messages"] if not isinstance(m, dict) and m.role == "assistant"]
    assert assistants and all(m.thinking is None for m in assistants)


def test_checkpoint_call_is_labelled_in_the_log(srv, monkeypatch):
    assert "phase=BUFFER" in draft_plan(srv, [step_text("false")])
    fake = FakeOllama(*[[chunk(calls=[tc("finish_step")])] for _ in range(srv.MAX_ATTEMPTS + 1)],
                      [chunk(content="PROBLEM: p")])
    monkeypatch.setattr(srv.ollama, "chat", fake)
    assert "phase=BUFFER" in srv.run_implementor()
    assert "step 1 checkpoint" in runlog(srv)
