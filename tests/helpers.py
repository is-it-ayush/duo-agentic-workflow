"""Shared helpers. Imported as `helpers` (pytest puts tests/ on sys.path)."""
import json, os, select, subprocess, time, types

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


def write_draft_handoff(srv, n):
    """What the drafter leaves for the next stage (required by the server)."""
    srv.HANDOFF.mkdir(parents=True, exist_ok=True)
    (srv.HANDOFF / "draft.md").write_text(f"DRAFT WRITTEN: {n} steps\ntests: written by the drafter\n")


def write_validate(srv, verdict="PASS", run=None):
    """What the validator leaves for the director. verdict: 'PASS' or 'FAIL step=<n>'."""
    srv.HANDOFF.mkdir(parents=True, exist_ok=True)
    run = srv.load().get("run_id") if run is None else run
    (srv.HANDOFF / "validate.md").write_text(f"{verdict} run={run}\nfindings: ...\n")


def draft_plan(srv, steps, mode="", pointer=0, handoff=True):
    """PLAN -> DRAFT -> write step files (+ drafter handoff) -> BUFFER. Returns fsm_to('BUFFER') output."""
    to_draft(srv)
    (srv.PLAN / "index.md").write_text("\n".join(f"{i:02d} step" for i in range(1, len(steps) + 1)))
    for i, text in enumerate(steps, 1):
        (srv.PLAN / f"{i:02d}.md").write_text(text)
    if handoff:
        write_draft_handoff(srv, len(steps))
    return srv.fsm_to("BUFFER", pointer=pointer, mode=mode)


def reach_validate(srv, monkeypatch, cmd="test -f ok.txt"):
    out = draft_plan(srv, [step_text(cmd)])
    assert "phase=BUFFER" in out, out
    monkeypatch.setattr(srv, "chat", FakeChat(*tool_pass()))
    out = srv.run_implementor()
    assert "phase=VALIDATE" in out, out


class Rpc:
    """Minimal MCP stdio client: newline-delimited JSON-RPC."""

    def __init__(self, argv, env, cwd):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, cwd=cwd)
        self.buf = b""

    def send(self, obj):
        self.p.stdin.write((json.dumps(obj) + "\n").encode())
        self.p.stdin.flush()

    def recv(self, want_id, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("id") == want_id:
                    return m
            if select.select([self.p.stdout], [], [], 0.5)[0]:
                chunk = os.read(self.p.stdout.fileno(), 65536)
                if not chunk:
                    break
                self.buf += chunk
        err = self.p.stderr.read().decode(errors="replace")[-800:] if self.p.poll() is not None else ""
        raise TimeoutError(f"no reply to id={want_id}; stderr: {err}")

    def handshake(self):
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        assert "serverInfo" in self.recv(1)["result"]
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, rid, method, params=None):
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return self.recv(rid)

    def close(self):
        self.p.kill()
        self.p.wait()
