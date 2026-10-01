"""Shared helpers. Imported as `helpers` (pytest puts tests/ on sys.path)."""
import types

PHASES = ["NONE", "PLAN", "DRAFT", "BUFFER", "IMPLEMENT", "VALIDATE", "DONE"]


def step_text(cmd="true", lock="", goal="g"):
    s = (f"GOAL: {goal}\nFILES: a.txt (new)\nDO:\n1. do it\n"
         f"TEST:\n```\n{cmd}\n```\nEXPECT: ok\n")
    return s + (f"LOCK: {lock}\n" if lock else "")


def call(name, **args):
    return types.SimpleNamespace(function=types.SimpleNamespace(name=name, arguments=args))


def reply(*calls, content=""):
    return types.SimpleNamespace(
        message=types.SimpleNamespace(tool_calls=list(calls) or None, content=content))


def tool_pass(path="ok.txt"):
    """Scripted Qwen: write `path`, then finish_step."""
    return [reply(call("write_file", path=path, content="x")), reply(call("finish_step"))]


class FakeChat:
    """Stand-in for server.chat. Replays `replies`, then `default`; records every message list."""

    def __init__(self, *replies, default=None):
        self.queue, self.default, self.calls = list(replies), default, []

    def __call__(self, msgs, tools=None):
        self.calls.append(list(msgs))
        if self.queue:
            return self.queue.pop(0)
        if self.default is not None:
            return self.default
        raise AssertionError("FakeChat exhausted: implementor made more calls than scripted")

    def first_user_messages(self):
        """Task message of every fresh context (system + user only)."""
        return [m[1]["content"] for m in self.calls if len(m) == 2]


def to_draft(srv):
    assert "phase=PLAN" in srv.fsm_to("PLAN")
    (srv.PLAN / "summary.md").write_text("summary")
    assert "phase=DRAFT" in srv.fsm_to("DRAFT")


def draft_plan(srv, steps, mode="", pointer=0):
    """PLAN -> DRAFT -> write step files -> BUFFER. Returns fsm_to('BUFFER') output."""
    to_draft(srv)
    (srv.PLAN / "index.md").write_text("\n".join(f"{i:02d} step" for i in range(1, len(steps) + 1)))
    for i, text in enumerate(steps, 1):
        (srv.PLAN / f"{i:02d}.md").write_text(text)
    return srv.fsm_to("BUFFER", pointer=pointer, mode=mode)


def reach_validate(srv, monkeypatch, cmd="test -f ok.txt"):
    out = draft_plan(srv, [step_text(cmd)])
    assert "phase=BUFFER" in out, out
    monkeypatch.setattr(srv, "chat", FakeChat(*tool_pass()))
    out = srv.run_implementor()
    assert "phase=VALIDATE" in out, out
