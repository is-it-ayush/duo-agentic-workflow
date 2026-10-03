"""Environment health: MCP registration, settings, hook, symlinks, and a real stdio boot of the server.
Reads the REAL ~/.claude.json and ~/.claude/settings.json (HOME can be overridden to test the tests)."""
import json, os, pathlib, select, subprocess, time
import pytest

AGENT = pathlib.Path(__file__).resolve().parents[1]
HOME = pathlib.Path.home()
pytestmark = pytest.mark.skipif(not (HOME / ".claude").is_dir(), reason="no ~/.claude")


def jload(p):
    assert p.is_file(), f"missing {p}"
    return json.loads(p.read_text())


def mcp_cfg():
    cfg = jload(HOME / ".claude.json").get("mcpServers", {}).get("agent")
    assert cfg, "MCP server 'agent' not registered at user scope (claude mcp add --scope user agent -- ...)"
    return cfg


def settings():
    return jload(HOME / ".claude/settings.json")


class Rpc:
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
        err = b""
        if self.p.poll() is not None:
            err = self.p.stderr.read()
        raise TimeoutError(f"no reply to id={want_id}; stderr: {err.decode(errors='replace')[-800:]}")

    def close(self):
        self.p.kill()
        self.p.wait()


def test_mcp_registration_points_at_this_repo(tmp_path):
    cfg = mcp_cfg()
    cmd = pathlib.Path(cfg["command"])
    assert cmd.is_file() and os.access(cmd, os.X_OK), f"python not found: {cmd}"
    assert pathlib.Path(cfg["args"][0]).resolve() == (AGENT / "server.py").resolve()


def test_mcp_server_boots_and_serves_tools(tmp_path):
    cfg = mcp_cfg()
    env = {**os.environ, **cfg.get("env", {}), "AGENT_PROJECT": str(tmp_path.resolve())}
    rpc = Rpc([cfg["command"], *cfg.get("args", [])], env, tmp_path)
    try:
        rpc.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
        assert "serverInfo" in rpc.recv(1)["result"]
        rpc.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        rpc.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in rpc.recv(2)["result"]["tools"]}
        assert {"fsm_status", "fsm_to", "run_implementor"} <= names, names
        rpc.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "fsm_status", "arguments": {}}})
        text = rpc.recv(3)["result"]["content"][0]["text"]
        assert "phase=NONE" in text and f"root={tmp_path.resolve()}" in text
    finally:
        rpc.close()


def test_permission_rules():
    perms = settings().get("permissions", {})
    deny = perms.get("deny", [])
    assert not [r for r in deny if r.startswith("Write(")], "Write() deny rules are rejected; use Edit()"
    assert any(r.startswith("Edit(") and "state.json" in r for r in deny), "state.json is not protected"
    for tool in ("fsm_status", "fsm_to", "run_implementor"):
        assert f"mcp__agent__{tool}" in perms.get("allow", []), tool
    dirs = [pathlib.Path(os.path.expanduser(d)).resolve() for d in perms.get("additionalDirectories", [])]
    assert AGENT.resolve() in dirs, "subagents can't read the prompt files without additionalDirectories"


def hook_command():
    entries = settings().get("hooks", {}).get("SessionStart", [])
    cmds = [h["command"] for e in entries for h in e.get("hooks", []) if h.get("type") == "command"]
    assert cmds, "no SessionStart command hook configured"
    return cmds[0]


def test_session_start_hook_injects_only_when_a_workflow_exists(tmp_path):
    cmd = hook_command()
    assert os.access(cmd.split()[0], os.X_OK), f"hook not executable: {cmd}"
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)}
    quiet = subprocess.run(cmd, shell=True, env=env, cwd=tmp_path, capture_output=True, text=True)
    assert quiet.returncode == 0 and quiet.stdout.strip() == ""
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent/state.json").write_text('{"phase":"PLAN"}')
    loud = subprocess.run(cmd, shell=True, env=env, cwd=tmp_path, capture_output=True, text=True)
    assert loud.returncode == 0 and "fsm_status" in loud.stdout


@pytest.mark.parametrize("rel,dest", [(f"director/agents/{n}.md", f".claude/agents/{n}.md")
                                      for n in ("drafter", "unblocker", "validator")]
                         + [("director/commands/agent.md", ".claude/commands/agent.md")])
def test_symlinks_into_claude_dir(rel, dest):
    link = HOME / dest
    assert link.exists(), f"missing {link}"
    assert link.resolve() == (AGENT / rel).resolve(), f"{link} does not point at {AGENT / rel}"


def test_session_start_hook_is_silent_inside_the_headless_implementor_session(tmp_path):
    """The headless Claude implementor would otherwise be told to 'follow director.md'."""
    cmd = hook_command()
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent/state.json").write_text('{"phase":"IMPLEMENT"}')
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path), "AGENT_IMPL_RUN": "1"}
    r = subprocess.run(cmd, shell=True, env=env, cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == ""
