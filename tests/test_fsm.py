import importlib, pathlib, subprocess, sys, types
import pytest

AGENT = pathlib.Path(__file__).resolve().parents[1]
STEP = "GOAL: g\nFILES: a.txt (new)\nDO:\n1. write a.txt\nTEST:\n```\n{cmd}\n```\nEXPECT: ok\n"

@pytest.fixture
def srv(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("AGENT_PROJECT", str(tmp_path))
    monkeypatch.syspath_prepend(str(AGENT))
    sys.modules.pop("server", None)
    return importlib.import_module("server")

def call(name, **args):
    return types.SimpleNamespace(function=types.SimpleNamespace(name=name, arguments=args))

def reply(*calls, content=""):
    return types.SimpleNamespace(message=types.SimpleNamespace(tool_calls=list(calls) or None, content=content))

def to_buffer(srv, cmd="true"):
    assert "phase=PLAN" in srv.fsm_to("PLAN")
    (srv.PLAN / "summary.md").write_text("x")
    assert "phase=DRAFT" in srv.fsm_to("DRAFT")
    (srv.PLAN / "index.md").write_text("01 g")
    (srv.PLAN / "01.md").write_text(STEP.format(cmd=cmd))
    assert "phase=BUFFER" in srv.fsm_to("BUFFER")

def test_illegal_moves(srv):
    assert srv.fsm_to("DRAFT").startswith("refused")
    assert srv.run_implementor().startswith("refused")
    to_buffer(srv)
    assert srv.fsm_to("DONE").startswith("refused")
    assert srv.fsm_to("IMPLEMENT").startswith("refused")

def test_plan_validation(srv):
    srv.fsm_to("PLAN"); (srv.PLAN / "summary.md").write_text("x"); srv.fsm_to("DRAFT")
    (srv.PLAN / "index.md").write_text("x"); (srv.PLAN / "01.md").write_text("GOAL: g\n")
    assert srv.fsm_to("BUFFER").startswith("refused")

def test_plan_immutable(srv):
    to_buffer(srv)
    (srv.PLAN / "01.md").write_text("tampered")
    assert "changed" in srv.run_implementor()

def test_happy_path(srv, monkeypatch):
    to_buffer(srv)
    script = iter([reply(call("write_file", path="a.txt", content="hi")), reply(call("finish_step"))])
    monkeypatch.setattr(srv, "chat", lambda *a, **k: next(script))
    assert "phase=VALIDATE" in srv.run_implementor()
    assert (srv.ROOT / "a.txt").read_text() == "hi"
    assert srv.load()["phase"] == "VALIDATE"

def test_escalation(srv, monkeypatch):
    to_buffer(srv, cmd="false")
    monkeypatch.setattr(srv, "chat", lambda *a, **k: reply(call("finish_step"), content="PROBLEM: x"))
    out = srv.run_implementor(); s = srv.load()
    assert "phase=BUFFER" in out and s["needs_directive"] and s["escalations"] == 1
    assert srv.CHECKPOINT.exists()
    assert srv.run_implementor().startswith("refused")      # directive.md missing
