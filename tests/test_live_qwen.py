"""AGENT_PROJECT=$(mktemp -d) ./venv/bin/python tests/live_qwen.py"""
import os, pathlib, subprocess, sys
import server as s

# this is a live test that runs the implementor with a real LLM, so it
# is not suitable for CI. It is meant to be run manually.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
root = pathlib.Path(os.environ["AGENT_PROJECT"])
subprocess.run(["git", "init", "-q"], cwd=root, check=True)

# setup a simple plan with one step that creates hello.py
s.fsm_to("PLAN"); (s.PLAN / "summary.md").write_text("x"); s.fsm_to("DRAFT")
(s.PLAN / "index.md").write_text("01 hello")
(s.PLAN / "01.md").write_text("GOAL: hello script\nFILES: hello.py (new)\nDO:\n1. Create hello.py that prints exactly: hi\nTEST:\n```\npython3 hello.py\n```\nEXPECT: exit 0\n")

# run the implementor and check that hello.py was created correctly
print(s.fsm_to("BUFFER")); print(s.run_implementor()); print((root / "hello.py").read_text())
