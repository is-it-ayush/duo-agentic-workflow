"""Real Ollama. Skipped unless LIVE=1:  LIVE=1 ./venv/bin/pytest -q tests/test_live_qwen.py -s
Non-deterministic by nature: a failure means 'look at .agent/run.log', not necessarily a bug."""
import os, subprocess
import pytest
from helpers import draft_plan, step_text

pytestmark = pytest.mark.skipif(not os.environ.get("LIVE"), reason="set LIVE=1 to hit Ollama")


def step(goal, files, do, cmd):
    return (f"GOAL: {goal}\nFILES: {files}\nDO:\n{do}\nTEST:\n```\n{cmd}\n```\nEXPECT: exit 0\n")


def test_live_single_step(srv):
    out = draft_plan(srv, [step("hello script", "hello.py (new)",
                                "1. Create hello.py that prints exactly: hi", "python3 hello.py")])
    assert "phase=BUFFER" in out, out
    out = srv.run_implementor()
    assert "phase=VALIDATE" in out, dump(srv, out)
    assert subprocess.run(["python3", "hello.py"], cwd=srv.ROOT, capture_output=True, text=True).stdout.strip() == "hi"


def test_live_two_steps_second_builds_on_first(srv):
    steps = [
        step("adder", "util.py (new)", "1. Create util.py with def add(a, b) returning a + b",
             "python3 -c \"import util; assert util.add(2, 3) == 5\""),
        step("main", "main.py (new)", "1. Create main.py that imports add from util and prints add(2, 3)",
             "python3 main.py"),
    ]
    assert "phase=BUFFER" in draft_plan(srv, steps)
    out = srv.run_implementor()
    assert "phase=VALIDATE" in out, dump(srv, out)
    assert srv.load()["pointer"] == 3
    assert subprocess.run(["python3", "main.py"], cwd=srv.ROOT, capture_output=True, text=True).stdout.strip() == "5"


def test_live_impossible_step_escalates_with_a_checkpoint(srv):
    impossible = step("cannot pass", "x.txt (new)", "1. Create x.txt", "python3 -c \"import sys; sys.exit(1)\"")
    assert "phase=BUFFER" in draft_plan(srv, [impossible])
    out = srv.run_implementor()
    assert "phase=BUFFER" in out and srv.CHECKPOINT.exists(), out
    assert srv.load()["needs_directive"] is True


def dump(srv, out):
    files = [srv.CHECKPOINT, *sorted(srv.ATTEMPTS.glob("*.log")), srv.AG / "run.log"]
    return out + "".join(f"\n=== {p.name} ===\n{p.read_text()[-2500:]}" for p in files if p.exists())
