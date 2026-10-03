#!/usr/bin/env python3
"""Stand-in for the `claude` CLI in tests (NOT a test module). It plays a script of invocations.

$FAKE_CLAUDE_SCRIPT: JSON list, one entry per invocation (the last entry repeats). Entry keys:
  actions: [{"tool": "write_file", "args": {...}}, ...]   executed with the real server tool code
  final: text of the result event          tools / extra_servers / model: what the init event reports
  delay_after_init / delay_before_tool / sleep: seconds        error: {"subtype": ..., "text": ...} to emit an error result
  no_init: true to skip the init event
Every invocation appends {argv, stdin, env, cwd} to <script>.calls."""
import json, os, pathlib, sys, time

script = pathlib.Path(os.environ["FAKE_CLAUDE_SCRIPT"])
calls = script.with_suffix(".calls")
stdin = sys.stdin.read()
entries = json.loads(script.read_text())
idx = len(calls.read_text().splitlines()) if calls.exists() else 0
entry = entries[min(idx, len(entries) - 1)]
argv = sys.argv[1:]
with open(calls, "a") as f:
    f.write(json.dumps({"argv": argv, "stdin": stdin, "cwd": os.getcwd(),
                        "env": {k: os.environ.get(k) for k in ("AGENT_IMPL_RUN", "AGENT_PROJECT")}}) + "\n")


def emit(o):
    print(json.dumps(o), flush=True)


TOOLS = ["read_file", "write_file", "run", "delete_path", "finish_step", "blocked"]
model = argv[argv.index("--model") + 1] if "--model" in argv else "?"
if not entry.get("no_init"):
    emit({"type": "system", "subtype": "init", "model": entry.get("model", model), "session_id": entry.get("session_id", "sess-fake"),
          "tools": entry.get("tools", [f"mcp__impl__{t}" for t in TOOLS]),
          "mcp_servers": [{"name": "impl", "status": entry.get("impl_status", "connected")}] + entry.get("extra_servers", [])})
time.sleep(entry.get("delay_after_init", 0))

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import server  # noqa: E402  (same tool code the MCP tool server runs)

for i, act in enumerate(entry.get("actions", [])):
    name, args = act["tool"], act.get("args", {})
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": f"mcp__impl__{name}", "input": args}]}})
    time.sleep(entry.get("delay_before_tool", 0))
    try:
        if name == "finish_step":
            out = server.impl_finish_step()
        elif name == "blocked":
            out = server.impl_blocked(**args)
        else:
            server.impl_guard()
            out = server.make_tools()[name](**args)
    except Exception as e:
        out = f"error: {e}"
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": out}]}})

err = entry.get("error")
emit({"type": "result", "subtype": err["subtype"] if err else "success", "is_error": bool(err),
      "result": err["text"] if err else entry.get("final", "done"), "num_turns": 1, "total_cost_usd": 0.0})
time.sleep(entry.get("sleep", 0))
sys.exit(entry.get("exit", 0))
