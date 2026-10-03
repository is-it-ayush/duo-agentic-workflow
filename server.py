#!/usr/bin/env python3
"""Director/implementor FSM + implementor runtime. Project = $AGENT_PROJECT or cwd."""
import hashlib, json, os, pathlib, re, shlex, shutil, signal, subprocess, sys, threading, time, types
from typing import Any
import ollama
from mcp.server.fastmcp import FastMCP

HOME = pathlib.Path(__file__).resolve().parent
ROOT = pathlib.Path(os.environ.get("AGENT_PROJECT", os.getcwd())).resolve()
AG = ROOT / ".agent"
STATE, PLAN = AG / "state.json", AG / "plan"
DIRECTIVE, CHECKPOINT, ATTEMPTS = AG / "directive.md", AG / "checkpoint.md", AG / "attempts"
PROGRESS, HANDOFF = AG / "progress.md", AG / "handoff"          # per-step summaries; stage-to-stage summaries
CTX_FILE, RESULT_FILE = AG / "impl_ctx.json", AG / "impl_result.json"   # server <-> headless implementor session

# Who implements the steps. Fixed when the server starts (AGENT_IMPLEMENTOR); no MCP tool or argument can change it,
# so neither the director nor the implementor can switch backends.
#   ollama: a local model; MODEL is its Ollama tag.
#   claude: a headless Claude Code session (your Claude Code login, no API key); MODEL is a Claude Code model
#           alias or name (haiku, sonnet, opus, claude-sonnet-4-6, ...) and must be set explicitly.
IMPLEMENTOR = os.environ.get("AGENT_IMPLEMENTOR", "ollama").strip().lower()      # "ollama" | "claude"
MODEL = os.environ.get("AGENT_MODEL", "qwen3:8b")                                # the one model setting
CLAUDE_BIN = os.environ.get("AGENT_CLAUDE_BIN", "claude")
CLAUDE_ATTEMPT_TIMEOUT = 1800                                                    # seconds per headless attempt
IMPL_TOOLS = ("read_file", "write_file", "run", "delete_path", "finish_step", "blocked")
NUM_CTX = 32768
MAX_ATTEMPTS = 4        # escalate when failed verifications exceed this
MAX_ESCALATIONS = 2     # director fixes per step before the user must be consulted
MAX_TOOL_CALLS = 40     # per attempt
STEP_CHAR_CAP = 1800
GIT_NAME = os.environ.get("AGENT_GIT_NAME", "agent-implementor")    # step commits use this identity,
GIT_EMAIL = os.environ.get("AGENT_GIT_EMAIL", "agent@localhost")    # never your own or your signing key
THINK = os.environ.get("AGENT_THINK", "0").lower() in ("1", "true", "yes")    # let the model think (slow); logged to run.log
STREAM = os.environ.get("AGENT_STREAM", "1").lower() in ("1", "true", "yes")  # stream tokens into run.log as they arrive
CTX = {"label": ""}                                                           # shown in run.log call headers
MAX_COMMIT_FILES = 200                                              # refuse suspiciously large commits
ALLOW = {"pytest", "python", "python3", "ruff", "make", "cargo", "npm", "go", "ls", "cat", "grep"}

EDGES = {  # (from, to) -> actor
    ("NONE", "PLAN"): "director",
    ("PLAN", "DRAFT"): "director",
    ("DRAFT", "BUFFER"): "director",
    ("BUFFER", "IMPLEMENT"): "director",
    ("IMPLEMENT", "IMPLEMENT"): "implementor",
    ("IMPLEMENT", "BUFFER"): "implementor",
    ("IMPLEMENT", "VALIDATE"): "implementor",
    ("VALIDATE", "BUFFER"): "director",
    ("VALIDATE", "DONE"): "director",
    ("DONE", "PLAN"): "director",
    ("BUFFER", "PLAN"): "director",
}
NEXT = {
    "NONE": "fsm_to('PLAN')",
    "PLAN": "discuss with user; write plan/summary.md; on APPROVE fsm_to('DRAFT')",
    "DRAFT": "spawn subagent 'drafter'; then fsm_to('BUFFER')",
    "BUFFER": "if needs_directive: spawn 'unblocker' (halted: ask user first); then run_implementor()",
    "IMPLEMENT": "implementor has control; wait",
    "VALIDATE": "spawn 'validator'; PASS -> fsm_to('DONE'); FAIL step=n -> fsm_to('BUFFER', pointer=n, mode='direct')",
    "DONE": "final summary to user; wait for user",
}
RUNNING = False


# ---------- state ----------
def load() -> dict[str, Any]:
    return json.loads(STATE.read_text()) if STATE.exists() else {"phase": "NONE"}

def save(s):
    AG.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))

def move(s, actor, to, **upd):
    if EDGES.get((s["phase"], to)) != actor:
        raise ValueError(f"illegal transition {s['phase']}->{to} for {actor}")
    s["phase"] = to
    s.update(upd)
    save(s)

def recover(s):
    """Server died or crashed mid-IMPLEMENT: hand control back to BUFFER."""
    if s["phase"] == "IMPLEMENT" and not RUNNING:
        move(s, "implementor", "BUFFER", needs_directive=False)

def trace(line):
    AG.mkdir(exist_ok=True)
    with open(AG / "run.log", "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {line}\n")


# ---------- plan ----------
def step_files():
    return sorted(PLAN.glob("[0-9][0-9].md"))

def plan_hash():
    h = hashlib.sha256()
    for p in sorted(PLAN.glob("*.md")):
        h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()

def parse_step(text):
    t = re.search(r"^TEST:\s*\n```[^\n]*\n(.*?)\n```", text, re.S | re.M)
    l = re.search(r"^LOCK:\s*(.+)$", text, re.M)
    return {
        "tests": [x.strip() for x in t.group(1).splitlines() if x.strip()] if t else [],
        "lock": [x.strip() for x in l.group(1).split(",") if x.strip()] if l else [],
    }

def check_plan():
    files = step_files()
    if not files: return "no step files"
    if not (PLAN / "index.md").exists(): return "missing index.md"
    for i, p in enumerate(files, 1):
        if p.name != f"{i:02d}.md": return f"numbering gap at {p.name}"
        t = p.read_text()
        if len(t) > STEP_CHAR_CAP: return f"{p.name}: {len(t)} chars > {STEP_CHAR_CAP}"
        for k in ("GOAL:", "FILES:", "DO:", "TEST:", "EXPECT:"):
            if not re.search(rf"^{k}", t, re.M): return f"{p.name}: missing {k}"
        if not parse_step(t)["tests"]: return f"{p.name}: empty TEST block"
    return None

def archive_plan():
    if not PLAN.exists(): return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = AG / "archive" / stamp
    k = 1
    while dst.exists():                      # two archives within one second must not collide
        dst = AG / "archive" / f"{stamp}-{k}"; k += 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLAN, dst)
    if PROGRESS.exists(): shutil.copy(PROGRESS, dst / "progress.md")
    if HANDOFF.exists(): shutil.copytree(HANDOFF, dst / "handoff")
    for f in step_files(): f.unlink()
    (PLAN / "index.md").unlink(missing_ok=True)


# ---------- verification ----------
def clean_env():
    """Environment for commands the implementor can trigger: no API keys, tokens or secrets."""
    bad = ("KEY", "TOKEN", "SECRET", "PASSWORD")
    return {k: v for k, v in os.environ.items()
            if not k.startswith("ANTHROPIC") and not any(b in k.upper() for b in bad)}

def snap(paths):
    return {p: (hashlib.sha256((ROOT / p).read_bytes()).hexdigest() if (ROOT / p).is_file() else None)
            for p in paths}

def verify(step_text, locks0):
    info = parse_step(step_text)
    if snap(info["lock"]) != locks0:
        return False, "LOCKED file modified. Revert it."
    out = []
    for c in info["tests"]:
        try:
            r = subprocess.run(shlex.split(c), cwd=ROOT, capture_output=True, text=True, timeout=300, env=clean_env())
            code, tail = r.returncode, (r.stdout + r.stderr)[-1500:]
        except Exception as e:
            code, tail = 1, str(e)
        out.append(f"$ {c}\nexit={code}\n{tail}")
        if code:
            return False, "\n".join(out)
    return True, "\n".join(out)

def log_fail(n, k, out):
    ATTEMPTS.mkdir(parents=True, exist_ok=True)
    stat = subprocess.run(["git", "diff", "--stat"], cwd=ROOT, capture_output=True, text=True).stdout[-800:]
    with open(ATTEMPTS / f"{n:02d}.log", "a") as f:
        f.write(f"--- attempt {k}\n{stat}\n{out[-1500:]}\n")


def git_commit(n, step, mode):
    """Commit the working tree (minus .agent/) as a plain, unsigned, non-personal identity.
    Never raises. Returns a short short-hash or a reason string."""
    env = {**os.environ, "GIT_AUTHOR_NAME": GIT_NAME, "GIT_AUTHOR_EMAIL": GIT_EMAIL,
           "GIT_COMMITTER_NAME": GIT_NAME, "GIT_COMMITTER_EMAIL": GIT_EMAIL}

    def git(*args):   # env beats config for identity; -c beats config for signing and hooks
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                              cwd=ROOT, capture_output=True, text=True, env=env, timeout=60)
    try:
        r = git("add", "-A", "--", ".", ":(exclude).agent")
        if r.returncode: return f"add failed: {r.stderr.strip()[:120]}"
        names = git("diff", "--cached", "--name-only").stdout.splitlines()
        if not names: return "nothing to commit"
        if len(names) > MAX_COMMIT_FILES:
            git("reset", "-q")
            return f"refused: {len(names)} files staged (> {MAX_COMMIT_FILES}); check .gitignore"
        m = re.search(r"^GOAL:\s*(.+)$", step, re.M)
        msg = f"{'fix ' if mode == 'direct' else ''}step {n}: {m.group(1).strip() if m else ''}".strip()
        r = git("commit", "--no-gpg-sign", "-m", msg, "-m", f"implementor: {impl_label()}")
        if r.returncode: return f"commit failed: {(r.stderr or r.stdout).strip()[:120]}"
        return git("rev-parse", "--short", "HEAD").stdout.strip()
    except Exception as e:
        return f"commit error: {e}"


# ---------- implementor runtime ----------
def system_prompt():
    parts = [(HOME / "common" / "style.md").read_text(),
             (HOME / "implementor" / "implementor.md").read_text()]
    a = ROOT / "AGENTS.md"
    if a.exists(): parts.append(a.read_text()[:3000])
    return "\n\n".join(parts)

def make_tools():
    def _safe(p, write=False):
        r = (ROOT / p).resolve()
        if r != ROOT and ROOT not in r.parents: raise ValueError("path escapes project root")
        if write and (r == AG or AG in r.parents): raise ValueError(".agent is read-only")
        return r

    def read_file(path: str) -> str:
        """Read a text file, path relative to project root."""
        return _safe(path).read_text()[:20000]

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file, path relative to project root."""
        p = _safe(path, write=True); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content); return "ok"

    def run(cmd: str) -> str:
        """Run an allowlisted command in the project root; returns exit code and output tail."""
        argv = shlex.split(cmd)
        if not argv or argv[0] not in ALLOW: return f"denied; allowed: {sorted(ALLOW)}"
        r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=300, env=clean_env())
        return f"exit={r.returncode}\n" + (r.stdout + r.stderr)[-3000:]

    def delete_path(path: str) -> str:
        """Delete a file or directory (recursively) inside the project. Never .git, .agent, or anything outside."""
        p = pathlib.Path(os.path.normpath(ROOT / path))           # lexical: folds '..' and absolute paths
        if ROOT not in p.parents: raise ValueError("path escapes project root")
        t = p.parent.resolve() / p.name                           # resolve the parent only, never the final
        if ROOT not in t.parents: raise ValueError("path escapes project root")   # component: a symlink is
        if t.relative_to(ROOT).parts[0] in (".git", ".agent"):                      # removed, not followed
            raise ValueError(f"{t.relative_to(ROOT).parts[0]} is protected")
        if not (t.is_symlink() or t.exists()): raise ValueError("no such path")
        if t.is_symlink() or t.is_file(): t.unlink()
        else: shutil.rmtree(t)
        return f"deleted {t.relative_to(ROOT)}"

    def finish_step() -> str:
        """Call when every DO item is done. Runs the step's TEST."""
        return ""

    def blocked(reason: str) -> str:
        """Call only if the step contradicts the code and cannot be done literally."""
        return ""

    return {f.__name__: f for f in (read_file, write_file, run, delete_path, finish_step, blocked)}

def chat(msgs, tools=None):
    if IMPLEMENTOR != "ollama":
        raise ValueError(f"chat() is the Ollama path; the {IMPLEMENTOR!r} implementor does not use it")
    return _chat_ollama(msgs, tools)

def _chat_ollama(msgs, tools=None):
    """One model call. Streams thinking / text / tool calls into .agent/run.log live (tail -f it).
    Returns an object with .message; thinking is logged but NOT kept in the history we send back."""
    AG.mkdir(exist_ok=True)
    kw = dict(model=MODEL, messages=msgs, tools=tools, think=THINK, keep_alive="30m",
              options={"num_ctx": NUM_CTX, "temperature": 0})
    content, calls, mode = [], [], None
    with open(AG / "run.log", "a") as f:
        f.write(f"\n── {time.strftime('%H:%M:%S')} {CTX['label']} [{impl_label()}] ──")

        def part(kind, text):
            nonlocal mode
            if kind != mode:
                f.write(f"\n[{kind}] "); mode = kind
            f.write(text); f.flush()

        for ch in (ollama.chat(stream=True, **kw) if STREAM else [ollama.chat(**kw)]):
            m = ch.message
            if m.thinking: part("think", m.thinking)
            if m.content:
                part("say", m.content); content.append(m.content)
            for tc in m.tool_calls or []:
                part("call", f"{tc.function.name}({json.dumps(dict(tc.function.arguments), default=str)[:300]})")
                calls.append(tc); mode = None
        f.write("\n")
    return types.SimpleNamespace(message=ollama.Message(role="assistant", content="".join(content),
                                                        tool_calls=calls or None))

def impl_label():
    if IMPLEMENTOR == "claude": return f"claude:{MODEL}"
    if IMPLEMENTOR == "ollama": return f"ollama:{MODEL}"
    return f"INVALID({IMPLEMENTOR})"

def backend_problem():
    """None if the selected implementor backend can run, else why not."""
    if IMPLEMENTOR == "ollama": return None
    if IMPLEMENTOR != "claude": return f"AGENT_IMPLEMENTOR must be 'ollama' or 'claude', got {IMPLEMENTOR!r}"
    if "AGENT_MODEL" not in os.environ:
        return "claude implementor needs AGENT_MODEL set to a Claude Code model alias or name (haiku, sonnet, opus, ...)"
    if not shutil.which(CLAUDE_BIN):
        return f"claude CLI not found ({CLAUDE_BIN!r}); install Claude Code or set AGENT_CLAUDE_BIN"
    return None


# ---------- handoffs: what each stage leaves for the next one ----------
def commit_files(rev):
    r = subprocess.run(["git", "show", "--name-only", "--format=", rev], cwd=ROOT, capture_output=True, text=True)
    return [x for x in r.stdout.split() if x][:6]

def record_progress(n, step, c, mode):
    """One line per finished step; the next step's fresh context is told about the last few."""
    m = re.search(r"^GOAL:\s*(.+)$", step, re.M)
    files = commit_files(c) if re.fullmatch(r"[0-9a-f]{4,40}", c) else []
    AG.mkdir(exist_ok=True)
    with open(PROGRESS, "a") as f:
        f.write(f"- {'fix ' if mode == 'direct' else ''}step {n}: {m.group(1).strip() if m else ''}"
                f" | files: {', '.join(files) or '-'} | commit: {c}\n")

def read_progress(k=5):
    return "\n".join(PROGRESS.read_text().splitlines()[-k:])[:1500] if PROGRESS.exists() else ""

def write_implement_handoff(s, outcome):
    """The implementor stage's summary for whoever comes next (unblocker / validator). Mechanical, no model."""
    HANDOFF.mkdir(parents=True, exist_ok=True)
    (HANDOFF / "implement.md").write_text(
        f"RUN {s.get('run_id', 0)}\nimplementor: {impl_label()}\nmode: {s['mode']}\noutcome: {outcome}\n\n"
        f"PROGRESS (this plan):\n{read_progress(50) or '-'}\n")

def build_task(s, n, step):
    task = f"STEP {n}/{s['total']}\n{step}"
    prog = read_progress()
    if prog: task += f"\n\nPREVIOUS STEPS (already done, do not redo):\n{prog}"
    if DIRECTIVE.exists(): task += "\n\nDIRECTIVE:\n" + DIRECTIVE.read_text()[:2000]
    return task

def check_draft_handoff(total):
    f = HANDOFF / "draft.md"
    if not f.exists(): return "handoff/draft.md missing (the drafter must write it)"
    text = f.read_text()
    if len(text) > 3000: return "handoff/draft.md too long (> 3000 chars)"
    m = re.fullmatch(r"DRAFT WRITTEN: (\d+) steps?", (text.splitlines() or [""])[0].strip())
    if not m: return "handoff/draft.md must start with 'DRAFT WRITTEN: <N> steps'"
    if int(m.group(1)) != total: return f"handoff/draft.md says {m.group(1)} steps but the plan has {total}"
    return None

def validate_verdict(s):
    """-> ((verdict, step|None), None) or (None, why)."""
    f = HANDOFF / "validate.md"
    if not f.exists(): return None, "handoff/validate.md missing (the validator must write it)"
    m = re.fullmatch(r"(?:(PASS)|FAIL step=(\d+)) run=(\d+)", (f.read_text().splitlines() or [""])[0].strip())
    if not m: return None, "handoff/validate.md must start with 'PASS run=<k>' or 'FAIL step=<n> run=<k>'"
    if int(m.group(3)) != s.get("run_id"):
        return None, f"handoff/validate.md is for run {m.group(3)}, the current run is {s.get('run_id')}"
    return (("PASS", None) if m.group(1) else ("FAIL", int(m.group(2)))), None

def reset_run_files():
    for f in (PROGRESS, CHECKPOINT, DIRECTIVE, CTX_FILE, RESULT_FILE):
        f.unlink(missing_ok=True)
    for d in (HANDOFF, ATTEMPTS):
        shutil.rmtree(d, ignore_errors=True)


# ---------- implementor tools for a headless Claude Code session (python server.py --impl-tools) ----------
def impl_guard():
    if RESULT_FILE.exists():
        raise ValueError("this step is already finished; stop and end your turn")

def impl_finish_step() -> str:
    impl_guard()
    ctx = json.loads(CTX_FILE.read_text())
    ok, out = verify((PLAN / f"{ctx['n']:02d}.md").read_text(), ctx["locks0"])
    if ok:
        RESULT_FILE.write_text(json.dumps({"status": "pass"}))
        return "PASS. Stop now; make no further changes."
    ctx["attempts"] += 1
    CTX_FILE.write_text(json.dumps(ctx))
    log_fail(ctx["n"], ctx["attempts"], out)
    if ctx["attempts"] > MAX_ATTEMPTS:
        RESULT_FILE.write_text(json.dumps({"status": "escalate"}))
        return "ESCALATE: attempt limit reached. Stop now."
    return f"FAIL attempt {ctx['attempts']}/{MAX_ATTEMPTS}\n{out}"

def impl_blocked(reason: str) -> str:
    impl_guard()
    ctx = json.loads(CTX_FILE.read_text())
    log_fail(ctx["n"], ctx["attempts"], "BLOCKED: " + reason)
    RESULT_FILE.write_text(json.dumps({"status": "blocked", "reason": reason}))
    return "Recorded. Stop now."

def serve_impl_tools():
    """MCP server handed to the headless implementor session. Same sandboxed tools as the Ollama loop;
    nothing from the director's server (no fsm_*, no run_implementor)."""
    app, t = FastMCP("impl"), make_tools()

    @app.tool()
    def read_file(path: str) -> str:
        """Read a text file, path relative to project root."""
        impl_guard(); return t["read_file"](path)

    @app.tool()
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file, path relative to project root."""
        impl_guard(); return t["write_file"](path, content)

    @app.tool()
    def run(cmd: str) -> str:
        """Run an allowlisted command in the project root; returns exit code and output tail."""
        impl_guard(); return t["run"](cmd)

    @app.tool()
    def delete_path(path: str) -> str:
        """Delete a file or directory (recursively) inside the project. Never .git, .agent, or anything outside."""
        impl_guard(); return t["delete_path"](path)

    @app.tool()
    def finish_step() -> str:
        """Call when every DO item is done. Runs the step's TEST."""
        return impl_finish_step()

    @app.tool()
    def blocked(reason: str) -> str:
        """Call only if the step contradicts the code and cannot be done literally."""
        return impl_blocked(reason)

    app.run()


# ---------- claude backend: a fresh headless Claude Code session per attempt ----------
def claude_argv():
    cfg = {"mcpServers": {"impl": {"command": sys.executable, "args": [str(HOME / "server.py"), "--impl-tools"],
                                   "env": {"AGENT_PROJECT": str(ROOT), "PATH": os.environ.get("PATH", ""),
                                           "HOME": os.environ.get("HOME", "")}}}}
    return [CLAUDE_BIN, "-p", "--model", MODEL, "--output-format", "stream-json", "--verbose",
            "--strict-mcp-config", "--mcp-config", json.dumps(cfg),
            "--tools", "", "--allowedTools", ",".join(f"mcp__impl__{x}" for x in IMPL_TOOLS),
            "--disallowedTools", "Bash,Edit,Write,MultiEdit,NotebookEdit,Read,Glob,Grep,WebFetch,WebSearch,Task,Agent",
            "--max-turns", str(MAX_TOOL_CALLS), "--append-system-prompt", system_prompt()]

FATAL = ("sandbox check", "model check", "impl MCP", "no init", "claude error")

def check_init(ev):
    """The session must have exactly our tools and the configured model, or we refuse to let it work."""
    extra = sorted(set(ev.get("tools") or []) - {f"mcp__impl__{x}" for x in IMPL_TOOLS})
    if extra: return f"sandbox check failed: unexpected tools {extra}"
    servers = {m.get("name"): m.get("status") for m in ev.get("mcp_servers") or []}
    if set(servers) - {"impl"}: return f"sandbox check failed: unexpected MCP servers {sorted(set(servers) - {'impl'})}"
    if servers.get("impl") == "failed": return "impl MCP server failed to start"
    mdl = str(ev.get("model") or "")
    if mdl and MODEL.lower() not in mdl.lower(): return f"model check failed: wanted {MODEL!r}, claude reports {mdl!r}"
    return ""

def launch_claude(task):
    """One fresh headless Claude Code session, task on stdin. Returns (final_text, problem)."""
    AG.mkdir(exist_ok=True)
    env = {**os.environ, "AGENT_IMPL_RUN": "1", "AGENT_PROJECT": str(ROOT)}
    final, problem, saw_init, tail, timed_out = "", "", False, [], []
    with open(AG / "run.log", "a") as log:
        def emit(kind, text):
            log.write(f"[{kind}] {text}\n"); log.flush()
        log.write(f"\n── {time.strftime('%H:%M:%S')} {CTX['label']} [{impl_label()}] ──\n"); log.flush()
        p = subprocess.Popen(claude_argv(), cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)

        def kill():
            try: os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        timer = threading.Timer(CLAUDE_ATTEMPT_TIMEOUT, lambda: (timed_out.append(1), kill()))
        timer.start()
        try:
            try:
                p.stdin.write(task); p.stdin.close()
            except BrokenPipeError:
                pass
            for raw in p.stdout:
                raw = raw.strip()
                if not raw: continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    tail = (tail + [raw])[-5:]; emit("raw", raw[:200]); continue
                kind = ev.get("type")
                if not saw_init and kind != "system":          # init must come first, or we cannot vouch for the session
                    problem = "no init event before the first message from claude (cannot verify sandbox or model)"
                    emit("abort", problem); kill(); break
                if kind == "system" and ev.get("subtype") == "init":
                    saw_init = True
                    problem = check_init(ev)
                    if problem: emit("abort", problem); kill(); break
                    if ev.get("session_id"):
                        emit("session", f"{ev['session_id']}  (look at it afterwards: claude --resume {ev['session_id']})")
                elif kind == "assistant":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if b.get("type") == "text" and b.get("text"): emit("say", b["text"])
                        elif b.get("type") == "tool_use":
                            emit("call", f"{b.get('name')}({json.dumps(b.get('input'), default=str)[:300]})")
                elif kind == "user":
                    for b in (ev.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            emit("result", str(b.get("content"))[:300])
                elif kind == "result":
                    final = str(ev.get("result") or "")
                    emit("usage", f"turns={ev.get('num_turns')} cost_usd={ev.get('total_cost_usd')} stop={ev.get('subtype')}")
                    if ev.get("is_error"):
                        problem = ("max turns reached" if ev.get("subtype") == "error_max_turns"
                                   else f"claude error: {final[:300]}")
        finally:
            timer.cancel(); kill(); rc = p.wait()
        if not problem:
            if timed_out: problem = f"timed out after {CLAUDE_ATTEMPT_TIMEOUT}s"
            elif not saw_init: problem = "no init event from claude (cannot verify sandbox or model): " + " | ".join(tail)[:300]
            elif rc not in (0, -signal.SIGKILL) and not final: problem = f"claude exited with {rc}: " + " | ".join(tail)[:300]
        if problem: emit("problem", problem)
    return final, problem

def run_step_claude(s, n, step):
    """Same contract as run_step. Every attempt is a new process, i.e. a new context window, which is told
    what the previous attempt did and why it failed."""
    CTX["label"] = f"step {n}"
    locks0 = snap(parse_step(step)["lock"])
    feedback = ""
    while True:
        RESULT_FILE.unlink(missing_ok=True)
        before = s["attempts"]
        CTX_FILE.write_text(json.dumps({"n": n, "attempts": before, "locks0": locks0}))
        text, problem = launch_claude(build_task(s, n, step) + feedback)
        s["attempts"] = json.loads(CTX_FILE.read_text())["attempts"]; save(s)
        res = json.loads(RESULT_FILE.read_text()) if RESULT_FILE.exists() else None
        RESULT_FILE.unlink(missing_ok=True)                 # consumed: a verdict must never leak into the next step
        if problem.startswith(FATAL):
            raise RuntimeError(problem)
        if res and res["status"] == "pass": return "pass", ""
        if res and res["status"] == "blocked": return "escalate", f"BLOCKED: {res.get('reason')}\n{text}"
        if res and res["status"] == "escalate": return "escalate", text
        ok, out = verify(step, locks0)                      # it stopped without a verdict: that stop is its finish_step
        if ok: return "pass", ""
        if s["attempts"] == before:                         # (a failed finish_step call already counted itself)
            s["attempts"] += 1; save(s)
            log_fail(n, s["attempts"], out + (f"\n(claude: {problem})" if problem else ""))
            if s["attempts"] > MAX_ATTEMPTS: return "escalate", text
        feedback = (f"\n\nPREVIOUS ATTEMPT FAILED ({s['attempts']}/{MAX_ATTEMPTS}):\n{out}\n"
                    f"Your last message was:\n{text[:800]}")


def run_step(s, n, step):
    """Fresh context per call. Returns ('pass'|'escalate', msgs)."""
    tools = make_tools()
    CTX["label"] = f"step {n}"
    locks0 = snap(parse_step(step)["lock"])
    msgs = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": build_task(s, n, step)}]
    calls, last = 0, ""
    while True:
        if calls >= MAX_TOOL_CALLS:
            s["attempts"] += 1; save(s); calls = 0
            log_fail(n, s["attempts"], f"tool-call budget exhausted; last reply: {last[:200]!r}")
            if s["attempts"] > MAX_ATTEMPTS: return "escalate", msgs
            msgs.append({"role": "user", "content": "Budget exhausted. Call finish_step now."})
        resp = chat(msgs, list(tools.values()))
        msgs.append(resp.message); calls += 1
        last = resp.message.content or ""
        if not resp.message.tool_calls:      # model stopped calling tools: treat as finish_step
            trace(f"step {n} text-only reply, verifying: {last[:200]!r}")
            ok, out = verify(step, locks0)
            if ok: return "pass", msgs
            s["attempts"] += 1; save(s); calls = 0
            log_fail(n, s["attempts"], out)
            if s["attempts"] > MAX_ATTEMPTS: return "escalate", msgs
            msgs.append({"role": "user", "content":
                         f"FAIL attempt {s['attempts']}/{MAX_ATTEMPTS}\n{out}\nFix it with tools, then call finish_step."})
            continue
        for tc in resp.message.tool_calls:
            name, args = tc.function.name, tc.function.arguments
            if name == "blocked":
                log_fail(n, s["attempts"], "BLOCKED: " + str(args.get("reason")))
                return "escalate", msgs
            if name == "finish_step":
                ok, out = verify(step, locks0)
                if ok: return "pass", msgs
                s["attempts"] += 1; save(s); calls = 0
                log_fail(n, s["attempts"], out)
                if s["attempts"] > MAX_ATTEMPTS: return "escalate", msgs
                result = f"FAIL attempt {s['attempts']}/{MAX_ATTEMPTS}\n{out}"
            else:
                try: result = str(tools[name](**args))
                except Exception as e: result = f"error: {e}"
            trace(f"step {n} {name} {str(args)[:120]} -> {result[:80]!r}")
            msgs.append({"role": "tool", "tool_name": name, "content": result})

def write_checkpoint(n, msgs):
    CTX["label"] = f"step {n} checkpoint"
    if isinstance(msgs, str):            # claude backend: no extra model call
        text = f"PROBLEM: step {n} did not pass.\nLAST MESSAGE FROM THE IMPLEMENTOR:\n{msgs[:1500]}"
    else:
        msgs = msgs + [{"role": "user", "content":
            "Stop. Write the checkpoint, nothing else:\nPROBLEM: <exact>\n"
            "ATTEMPTS: one line each '<k>: changed <what> -> <result>'\nSTUCK ON: <one line>"}]
        try:
            text = (chat(msgs).message.content or "")[:2000]
        except Exception as e:
            text = f"(checkpoint model call failed: {e})"
    lg = ATTEMPTS / f"{n:02d}.log"
    CHECKPOINT.write_text(f"STEP {n}\n{text}\n\nLOG\n{lg.read_text()[-2500:] if lg.exists() else ''}")

def handoff(msg, commits):
    return f"{msg} commits={','.join(commits) or '-'} implementor={impl_label()}"

def implement(s):
    commits = []
    runner = run_step_claude if IMPLEMENTOR == "claude" else run_step
    while True:
        n = s["pointer"]
        step = (PLAN / f"{n:02d}.md").read_text()
        res, msgs = runner(s, n, step)
        if res == "escalate":
            write_checkpoint(n, msgs)
            DIRECTIVE.unlink(missing_ok=True)
            write_implement_handoff(s, f"escalated at step {n}; see .agent/checkpoint.md")
            esc = s["escalations"] + 1
            move(s, "implementor", "BUFFER", attempts=0, escalations=esc,
                 needs_directive=True, halted=esc > MAX_ESCALATIONS)
            return handoff(f"HANDOFF implementor->director phase=BUFFER step={n} "
                           f"escalation={esc}/{MAX_ESCALATIONS} halted={s['halted']} "
                           f"checkpoint=.agent/checkpoint.md", commits)
        DIRECTIVE.unlink(missing_ok=True)
        (ATTEMPTS / f"{n:02d}.log").unlink(missing_ok=True)
        c = git_commit(n, step, s["mode"])
        trace(f"step {n} commit: {c}")
        record_progress(n, step, c, s["mode"])
        commits.append(f"{n}:{c}")
        if s["mode"] == "direct":
            write_implement_handoff(s, f"fix of step {n} passed (direct mode)")
            move(s, "implementor", "VALIDATE", attempts=0, escalations=0)
            return handoff(f"HANDOFF implementor->director phase=VALIDATE step={n} (direct) passed", commits)
        if n < s["total"]:
            move(s, "implementor", "IMPLEMENT", pointer=n + 1, attempts=0, escalations=0)
        else:
            write_implement_handoff(s, f"all {n} steps passed")
            move(s, "implementor", "VALIDATE", pointer=n + 1, attempts=0, escalations=0)
            return handoff(f"HANDOFF implementor->director phase=VALIDATE steps 1..{n} passed", commits)


# ---------- MCP tools (director side) ----------
mcp = FastMCP("agent")

@mcp.tool()
def fsm_status() -> str:
    """Current workflow state and the next legal action."""
    s = load(); recover(s)
    p = s["phase"]
    prob = backend_problem()
    return (f"root={ROOT}\nimplementor={impl_label()}{' (UNUSABLE: ' + prob + ')' if prob else ''}\nphase={p} mode={s.get('mode','-')} pointer={s.get('pointer','-')}/{s.get('total','-')} "
            f"attempts={s.get('attempts',0)} esc={s.get('escalations',0)} halted={s.get('halted',False)} "
            f"needs_directive={s.get('needs_directive',False)}\nnext: {NEXT[p]}")

@mcp.tool()
def fsm_to(phase: str, pointer: int = 0, mode: str = "") -> str:
    """Director transitions: PLAN, DRAFT, BUFFER, DONE. IMPLEMENT is entered via run_implementor.
    BUFFER from VALIDATE requires pointer=<step> and mode='direct'."""
    s = load(); recover(s); frm = s["phase"]
    if EDGES.get((frm, phase)) != "director" or phase == "IMPLEMENT":
        return f"refused: {frm}->{phase}"
    if mode and mode not in ("progressive", "direct"):
        return "refused: mode must be progressive|direct"
    if phase == "PLAN" and frm == "NONE" and (ROOT == pathlib.Path.home() or not (ROOT / ".git").exists()):
        return "refused: run inside a git repo (not ~)"
    if phase == "PLAN":
        if frm != "NONE": archive_plan()
        reset_run_files()
        PLAN.mkdir(parents=True, exist_ok=True)
        move(s, "director", "PLAN", mode="progressive", pointer=1, total=0, attempts=0,
             escalations=0, needs_directive=False, halted=False, plan_hash=None, run_id=0)
    elif phase == "DRAFT":
        f = PLAN / "summary.md"
        if not f.exists() or not f.read_text().strip(): return "refused: plan/summary.md missing"
        move(s, "director", "DRAFT")
    elif phase == "BUFFER" and frm == "DRAFT":
        err = check_plan() or check_draft_handoff(len(step_files()))
        if err: return f"refused: {err}"
        total = len(step_files()); ptr = pointer or 1
        if not 1 <= ptr <= total: return "refused: bad pointer"
        move(s, "director", "BUFFER", total=total, pointer=ptr, mode=mode or "progressive",
             plan_hash=plan_hash(), needs_directive=False, attempts=0, escalations=0, halted=False)
    elif phase == "BUFFER":     # from VALIDATE
        if mode != "direct" or not 1 <= pointer <= s["total"]:
            return "refused: need pointer=<step> and mode='direct'"
        if not DIRECTIVE.exists(): return "refused: directive.md missing"
        verdict, why = validate_verdict(s)
        if why: return f"refused: {why}"
        if verdict != ("FAIL", pointer):
            return f"refused: validator verdict is {verdict[0]}{'' if verdict[1] is None else f' step={verdict[1]}'}; need FAIL step={pointer}"
        move(s, "director", "BUFFER", pointer=pointer, mode="direct", needs_directive=False,
             attempts=0, escalations=0, halted=False)
    elif phase == "DONE":
        verdict, why = validate_verdict(s)
        if why: return f"refused: {why}"
        if verdict[0] != "PASS":
            return f"refused: validator said FAIL step={verdict[1]}; use fsm_to('BUFFER', pointer={verdict[1]}, mode='direct')"
        move(s, "director", "DONE")
    return fsm_status()

@mcp.tool()
def run_implementor(user_guided: bool = False) -> str:
    """Hand control to the local implementor (BUFFER only). Blocks; returns one HANDOFF line.
    user_guided=true only after the user answered an escalation-limit halt."""
    global RUNNING
    s = load()
    if s["phase"] != "BUFFER": return f"refused: phase={s['phase']}"
    bad = backend_problem()
    if bad: return f"refused: {bad}"
    if plan_hash() != s["plan_hash"]: return "refused: plan files changed since DRAFT"
    if s["needs_directive"] and not DIRECTIVE.exists(): return "refused: directive.md missing"
    if s["halted"] and not user_guided: return "refused: escalation limit; consult the user, then user_guided=true"
    move(s, "director", "IMPLEMENT", needs_directive=False, halted=False, implementor=impl_label(),
         run_id=s.get("run_id", 0) + 1,
         **({"escalations": 0} if user_guided else {}))
    RUNNING = True
    try:
        return implement(s)
    except Exception as e:
        if s["phase"] == "IMPLEMENT":
            move(s, "implementor", "BUFFER", needs_directive=False)
        return f"HANDOFF implementor->director phase=BUFFER ERROR={e}; fix environment, call run_implementor() again"
    finally:
        RUNNING = False

if __name__ == "__main__":
    if "--impl-tools" in sys.argv: serve_impl_tools()      # spawned by the headless implementor session
    else: mcp.run()
