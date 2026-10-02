#!/usr/bin/env python3
"""Director/implementor FSM + implementor runtime. Project = $AGENT_PROJECT or cwd."""
import hashlib, json, os, pathlib, re, shlex, shutil, subprocess, time
from typing import Any
import ollama
from mcp.server.fastmcp import FastMCP

HOME = pathlib.Path(__file__).resolve().parent
ROOT = pathlib.Path(os.environ.get("AGENT_PROJECT", os.getcwd())).resolve()
AG = ROOT / ".agent"
STATE, PLAN = AG / "state.json", AG / "plan"
DIRECTIVE, CHECKPOINT, ATTEMPTS = AG / "directive.md", AG / "checkpoint.md", AG / "attempts"

MODEL = os.environ.get("AGENT_MODEL", "qwen3:8b")
NUM_CTX = 32768
MAX_ATTEMPTS = 4        # escalate when failed verifications exceed this
MAX_ESCALATIONS = 2     # director fixes per step before the user must be consulted
MAX_TOOL_CALLS = 40     # per attempt
STEP_CHAR_CAP = 1800
GIT_NAME = os.environ.get("AGENT_GIT_NAME", "agent-implementor")    # step commits use this identity,
GIT_EMAIL = os.environ.get("AGENT_GIT_EMAIL", "agent@localhost")    # never your own or your signing key
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
    dst = AG / "archive" / time.strftime("%Y%m%d-%H%M%S")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLAN, dst)
    for f in step_files(): f.unlink()
    (PLAN / "index.md").unlink(missing_ok=True)


# ---------- verification ----------
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
            r = subprocess.run(shlex.split(c), cwd=ROOT, capture_output=True, text=True, timeout=300)
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
        r = git("commit", "--no-gpg-sign", "-m", msg)
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
        r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=300)
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
    return ollama.chat(model=MODEL, messages=msgs, tools=tools, think=False, keep_alive="30m",
                       options={"num_ctx": NUM_CTX, "temperature": 0})

def run_step(s, n, step):
    """Fresh context per call. Returns ('pass'|'escalate', msgs)."""
    tools = make_tools()
    locks0 = snap(parse_step(step)["lock"])
    task = f"STEP {n}/{s['total']}\n{step}"
    if DIRECTIVE.exists():
        task += "\n\nDIRECTIVE:\n" + DIRECTIVE.read_text()[:2000]
    msgs = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": task}]
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
    msgs = msgs + [{"role": "user", "content":
        "Stop. Write the checkpoint, nothing else:\nPROBLEM: <exact>\n"
        "ATTEMPTS: one line each '<k>: changed <what> -> <result>'\nSTUCK ON: <one line>"}]
    text = (chat(msgs).message.content or "")[:2000]
    lg = ATTEMPTS / f"{n:02d}.log"
    CHECKPOINT.write_text(f"STEP {n}\n{text}\n\nLOG\n{lg.read_text()[-2500:] if lg.exists() else ''}")

def handoff(msg, commits):
    return f"{msg} commits={','.join(commits) or '-'}"

def implement(s):
    commits = []
    while True:
        n = s["pointer"]
        step = (PLAN / f"{n:02d}.md").read_text()
        res, msgs = run_step(s, n, step)
        if res == "escalate":
            write_checkpoint(n, msgs)
            DIRECTIVE.unlink(missing_ok=True)
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
        commits.append(f"{n}:{c}")
        if s["mode"] == "direct":
            move(s, "implementor", "VALIDATE", attempts=0, escalations=0)
            return handoff(f"HANDOFF implementor->director phase=VALIDATE step={n} (direct) passed", commits)
        if n < s["total"]:
            move(s, "implementor", "IMPLEMENT", pointer=n + 1, attempts=0, escalations=0)
        else:
            move(s, "implementor", "VALIDATE", pointer=n + 1, attempts=0, escalations=0)
            return handoff(f"HANDOFF implementor->director phase=VALIDATE steps 1..{n} passed", commits)


# ---------- MCP tools (director side) ----------
mcp = FastMCP("agent")

@mcp.tool()
def fsm_status() -> str:
    """Current workflow state and the next legal action."""
    s = load(); recover(s)
    p = s["phase"]
    return (f"root={ROOT}\nphase={p} mode={s.get('mode','-')} pointer={s.get('pointer','-')}/{s.get('total','-')} "
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
        PLAN.mkdir(parents=True, exist_ok=True)
        move(s, "director", "PLAN", mode="progressive", pointer=1, total=0, attempts=0,
             escalations=0, needs_directive=False, halted=False, plan_hash=None)
    elif phase == "DRAFT":
        f = PLAN / "summary.md"
        if not f.exists() or not f.read_text().strip(): return "refused: plan/summary.md missing"
        move(s, "director", "DRAFT")
    elif phase == "BUFFER" and frm == "DRAFT":
        err = check_plan()
        if err: return f"refused: {err}"
        total = len(step_files()); ptr = pointer or 1
        if not 1 <= ptr <= total: return "refused: bad pointer"
        move(s, "director", "BUFFER", total=total, pointer=ptr, mode=mode or "progressive",
             plan_hash=plan_hash(), needs_directive=False, attempts=0, escalations=0, halted=False)
    elif phase == "BUFFER":     # from VALIDATE
        if mode != "direct" or not 1 <= pointer <= s["total"]:
            return "refused: need pointer=<step> and mode='direct'"
        if not DIRECTIVE.exists(): return "refused: directive.md missing"
        move(s, "director", "BUFFER", pointer=pointer, mode="direct", needs_directive=False,
             attempts=0, escalations=0, halted=False)
    elif phase == "DONE":
        move(s, "director", "DONE")
    return fsm_status()

@mcp.tool()
def run_implementor(user_guided: bool = False) -> str:
    """Hand control to the local implementor (BUFFER only). Blocks; returns one HANDOFF line.
    user_guided=true only after the user answered an escalation-limit halt."""
    global RUNNING
    s = load()
    if s["phase"] != "BUFFER": return f"refused: phase={s['phase']}"
    if plan_hash() != s["plan_hash"]: return "refused: plan files changed since DRAFT"
    if s["needs_directive"] and not DIRECTIVE.exists(): return "refused: directive.md missing"
    if s["halted"] and not user_guided: return "refused: escalation limit; consult the user, then user_guided=true"
    move(s, "director", "IMPLEMENT", needs_directive=False, halted=False,
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
    mcp.run()
