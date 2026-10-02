"""delete_path sandbox + server-side step commits (unsigned, non-personal identity)."""
import os, stat, subprocess
import pytest
from helpers import FakeChat, call, draft_plan, reply, step_text, tool_pass

NAME, EMAIL = "agent-implementor", "agent@localhost"


def git(srv, *a, check=True):
    return subprocess.run(["git", *a], cwd=srv.ROOT, capture_output=True, text=True, check=check).stdout.strip()


def commits(srv):
    r = subprocess.run(["git", "log", "--format=%s"], cwd=srv.ROOT, capture_output=True, text=True)
    return r.stdout.splitlines() if r.returncode == 0 else []


def hostile_git_config(srv):
    """Mimic: signing required, personal identity, signer that always fails."""
    for k, v in [("commit.gpgsign", "true"), ("user.signingkey", "DEADBEEF"), ("gpg.program", "/bin/false"),
                 ("user.name", "Personal Me"), ("user.email", "me@example.com")]:
        git(srv, "config", k, v)
    plain = subprocess.run(["git", "commit", "--allow-empty", "-m", "x"], cwd=srv.ROOT, capture_output=True, text=True)
    assert plain.returncode != 0, "sanity: a plain commit must fail in this config"


def build(srv, n=1, mode="", pointer=0):
    steps = [step_text(f"test -f a{i}.txt", goal=f"goal {i}") for i in range(1, n + 1)]
    assert "phase=BUFFER" in draft_plan(srv, steps, mode=mode, pointer=pointer)


def run_with(monkeypatch, srv, *replies, default=None):
    monkeypatch.setattr(srv, "chat", FakeChat(*replies, default=default))
    return srv.run_implementor()


# ---------------- delete_path ----------------
@pytest.fixture
def tools(srv):
    return srv.make_tools()


def test_delete_file_and_directory_tree(srv, tools):
    (srv.ROOT / "a.txt").write_text("x")
    (srv.ROOT / "d/e").mkdir(parents=True)
    (srv.ROOT / "d/e/f.txt").write_text("x")
    assert tools["delete_path"]("a.txt") == "deleted a.txt" and not (srv.ROOT / "a.txt").exists()
    assert tools["delete_path"]("d") == "deleted d" and not (srv.ROOT / "d").exists()
    assert srv.ROOT.exists()


def test_delete_never_follows_symlinks_out_of_the_project(srv, tools, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "f.txt").write_text("keep")
    (outside / "dir").mkdir(); (outside / "dir/g.txt").write_text("keep")
    os.symlink(outside / "f.txt", srv.ROOT / "link_file")
    os.symlink(outside / "dir", srv.ROOT / "link_dir")
    (srv.ROOT / "inner").mkdir()
    os.symlink(outside / "dir", srv.ROOT / "inner/escape")
    tools["delete_path"]("link_file")                       # removes the link only
    tools["delete_path"]("link_dir")                        # removes the link only
    assert not os.path.lexists(srv.ROOT / "link_file") and not os.path.lexists(srv.ROOT / "link_dir")
    with pytest.raises(ValueError, match="escapes"):        # path *through* a symlinked dir
        os.symlink(outside / "dir", srv.ROOT / "via")
        tools["delete_path"]("via/g.txt")
    tools["delete_path"]("inner")                           # rmtree must not follow inner/escape
    assert (outside / "f.txt").read_text() == "keep" and (outside / "dir/g.txt").read_text() == "keep"


def test_delete_refuses_outside_root_and_root_itself(srv, tools, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    victim = outside / "v.txt"; victim.write_text("keep")
    rel = os.path.relpath(victim, srv.ROOT)
    for bad in (rel, str(victim), "../", "..", ".", "", "sub/..", "/"):
        with pytest.raises(ValueError, match="escapes"):
            tools["delete_path"](bad)
    assert victim.exists() and srv.ROOT.exists()


@pytest.mark.parametrize("bad", [".git", "./.git", ".git/HEAD", ".agent", ".agent/plan/01.md", "sub/../.git"])
def test_delete_protects_git_and_agent_dirs(srv, tools, bad):
    srv.PLAN.mkdir(parents=True, exist_ok=True); (srv.PLAN / "01.md").write_text("plan")
    (srv.ROOT / "sub").mkdir()
    with pytest.raises(ValueError, match="protected"):
        tools["delete_path"](bad)
    assert (srv.ROOT / ".git" / "HEAD").exists() and (srv.PLAN / "01.md").exists()


def test_delete_missing_path_is_an_error(srv, tools):
    with pytest.raises(ValueError, match="no such path"):
        tools["delete_path"]("nope.txt")


def test_stray_file_cleanup_unblocks_the_step(srv, monkeypatch):
    """The original bug: a file Qwen created and could not delete made the step unpassable."""
    assert "phase=BUFFER" in draft_plan(srv, [step_text("test ! -e stray.txt")])
    out = run_with(monkeypatch, srv,
                   reply(call("write_file", path="stray.txt", content="x")),
                   reply(call("delete_path", path="stray.txt")),
                   reply(call("finish_step")))
    assert "phase=VALIDATE" in out and not (srv.ROOT / "stray.txt").exists()


# ---------------- step commits ----------------
def test_commit_is_unsigned_and_uses_the_agent_identity(srv, monkeypatch):
    hostile_git_config(srv)
    build(srv, 1)
    out = run_with(monkeypatch, srv, *tool_pass("a1.txt"))
    assert "phase=VALIDATE" in out and "commits=1:" in out and "failed" not in out
    fmt = git(srv, "log", "-1", "--format=%an|%ae|%cn|%ce|%G?|%s")
    assert fmt == f"{NAME}|{EMAIL}|{NAME}|{EMAIL}|N|step 1: goal 1"
    assert git(srv, "show", "--name-only", "--format=", "HEAD") == "a1.txt"


def test_ambient_git_identity_env_vars_do_not_leak_into_commits(srv, monkeypatch):
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "Personal Env")
    build(srv, 1)
    run_with(monkeypatch, srv, *tool_pass("a1.txt"))
    assert git(srv, "log", "-1", "--format=%an|%ae|%cn|%ce") == f"{NAME}|{EMAIL}|{NAME}|{EMAIL}"


def test_one_commit_per_step_and_agent_dir_is_never_committed(srv, monkeypatch):
    build(srv, 3)
    run_with(monkeypatch, srv, *[r for i in (1, 2, 3) for r in tool_pass(f"a{i}.txt")])
    assert commits(srv) == ["step 3: goal 3", "step 2: goal 2", "step 1: goal 1"]
    assert git(srv, "show", "--name-only", "--format=", "HEAD") == "a3.txt"
    assert ".agent" not in git(srv, "ls-files")


def test_direct_mode_commit_is_labelled_as_a_fix(srv, monkeypatch):
    build(srv, 2, mode="direct", pointer=2)
    run_with(monkeypatch, srv, *tool_pass("a2.txt"))
    assert commits(srv) == ["fix step 2: goal 2"]


def test_step_without_changes_makes_no_commit(srv, monkeypatch):
    assert "phase=BUFFER" in draft_plan(srv, [step_text("true")])
    out = run_with(monkeypatch, srv, reply(content="nothing to do"))
    assert "phase=VALIDATE" in out and "nothing to commit" in out and commits(srv) == []


def test_failed_steps_are_not_committed(srv, monkeypatch):
    assert "phase=BUFFER" in draft_plan(srv, [step_text("false")])
    out = run_with(monkeypatch, srv, default=reply(call("write_file", path="junk.txt", content="x"),
                                                 call("finish_step")))
    assert "phase=BUFFER" in out and commits(srv) == []


def test_user_hooks_cannot_block_the_commit(srv, monkeypatch):
    hook = srv.ROOT / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    build(srv, 1)
    run_with(monkeypatch, srv, *tool_pass("a1.txt"))
    assert commits(srv) == ["step 1: goal 1"]


def test_oversized_commit_is_refused_and_unstaged(srv, monkeypatch):
    monkeypatch.setattr(srv, "MAX_COMMIT_FILES", 2)
    build(srv, 1)
    out = run_with(monkeypatch, srv, reply(*[call("write_file", path=f"f{i}.txt", content="x") for i in range(3)],
                                           call("write_file", path="a1.txt", content="x"), call("finish_step")))
    assert "phase=VALIDATE" in out and "refused" in out and ".gitignore" in out
    assert commits(srv) == [] and git(srv, "diff", "--cached", "--name-only") == ""


def test_commit_failure_is_reported_but_never_blocks_the_workflow(srv, monkeypatch):
    (srv.ROOT / ".git/index.lock").write_text("")              # makes `git add` fail
    build(srv, 1)
    out = run_with(monkeypatch, srv, *tool_pass("a1.txt"))
    assert "phase=VALIDATE" in out and "add failed" in out
    assert srv.load()["phase"] == "VALIDATE"
