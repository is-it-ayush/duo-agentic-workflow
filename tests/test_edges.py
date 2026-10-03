"""FSM structure: edge table, actor enforcement, plan gating, lifecycle. No Ollama, no Claude."""
import itertools
import pytest
from helpers import (PHASES, FakeChat, draft_plan, reach_validate, step_text, to_draft, tool_pass,
                     write_draft_handoff, write_validate)

TARGETS = ["PLAN", "DRAFT", "BUFFER", "IMPLEMENT", "VALIDATE", "DONE", "BOGUS"]


def forced(srv, phase, **kw):
    s = {"phase": phase, "mode": "progressive", "pointer": 1, "total": 1, "attempts": 0,
         "escalations": 0, "needs_directive": False, "halted": False, "plan_hash": "x"}
    s.update(kw)
    srv.save(s)


# ---------- edge table ----------
def test_edge_table_is_well_formed(srv):
    assert set(srv.NEXT) == set(PHASES)
    for (a, b), actor in srv.EDGES.items():
        assert a in PHASES and b in PHASES and b != "NONE"
        assert actor in ("director", "implementor")


@pytest.mark.parametrize("a,b", list(itertools.product(PHASES, PHASES)))
def test_move_allows_only_listed_edges_for_the_listed_actor(srv, a, b):
    for actor in ("director", "implementor"):
        s = {"phase": a}
        if srv.EDGES.get((a, b)) == actor:
            srv.move(s, actor, b)
            assert s["phase"] == b
        else:
            with pytest.raises(ValueError):
                srv.move(s, actor, b)
            assert s["phase"] == a


@pytest.mark.parametrize("src", PHASES)
def test_fsm_to_refuses_every_illegal_edge(srv, monkeypatch, src):
    monkeypatch.setattr(srv, "RUNNING", True)      # keep recover() from rewriting IMPLEMENT
    for tgt in TARGETS:
        forced(srv, src)
        out = srv.fsm_to(tgt)
        if srv.EDGES.get((src, tgt)) != "director" or tgt == "IMPLEMENT":
            assert out.startswith("refused"), (src, tgt, out)
            assert srv.load()["phase"] == src


def test_stale_implement_is_recovered_to_buffer(srv):
    forced(srv, "IMPLEMENT")                       # e.g. server was killed mid-run
    assert "phase=BUFFER" in srv.fsm_status()
    assert srv.load()["needs_directive"] is False


def test_plan_refused_outside_a_git_repo(load_server):
    srv = load_server(git=False)
    assert srv.fsm_to("PLAN").startswith("refused")
    assert not srv.STATE.exists()


# ---------- PLAN -> DRAFT -> BUFFER gating ----------
def test_draft_requires_nonempty_summary(srv):
    srv.fsm_to("PLAN")
    assert srv.fsm_to("DRAFT").startswith("refused")
    (srv.PLAN / "summary.md").write_text("  \n")
    assert srv.fsm_to("DRAFT").startswith("refused")


V = step_text()


def _without(text, key):
    return "\n".join(l for l in text.splitlines() if not l.startswith(key)) + "\n"


CASES = [
    ("no-steps", {"index.md": "x"}, "no step files"),
    ("no-index", {"01.md": V}, "missing index.md"),
    ("numbering-gap", {"index.md": "x", "01.md": V, "03.md": V}, "numbering gap"),
    ("too-long", {"index.md": "x", "01.md": V + "x" * 2000}, "chars"),
    ("no-GOAL", {"index.md": "x", "01.md": _without(V, "GOAL:")}, "missing GOAL:"),
    ("no-FILES", {"index.md": "x", "01.md": _without(V, "FILES:")}, "missing FILES:"),
    ("no-DO", {"index.md": "x", "01.md": _without(V, "DO:")}, "missing DO:"),
    ("no-TEST", {"index.md": "x", "01.md": _without(V, "TEST:")}, "missing TEST:"),
    ("no-EXPECT", {"index.md": "x", "01.md": _without(V, "EXPECT:")}, "missing EXPECT:"),
    ("empty-test-block", {"index.md": "x", "01.md": V.replace("true", "")}, "empty TEST"),
    ("test-without-fence", {"index.md": "x", "01.md": V.replace("```\ntrue\n```\n", "true\n")}, "empty TEST"),
]


@pytest.mark.parametrize("files,msg", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_malformed_plans_are_rejected(srv, files, msg):
    to_draft(srv)
    for name, text in files.items():
        (srv.PLAN / name).write_text(text)
    out = srv.fsm_to("BUFFER")
    assert out.startswith("refused") and msg in out, out
    assert srv.load()["phase"] == "DRAFT"


def test_buffer_args_are_validated(srv):
    to_draft(srv)
    (srv.PLAN / "index.md").write_text("x")
    (srv.PLAN / "01.md").write_text(V)
    for kw in ({"mode": "bogus"}, {"pointer": 2}, {"pointer": -1}):
        assert srv.fsm_to("BUFFER", **kw).startswith("refused"), kw
    assert srv.load()["phase"] == "DRAFT"
    assert srv.fsm_to("BUFFER", pointer=1, mode="direct").startswith("refused")        # drafter handoff missing
    write_draft_handoff(srv, 1)
    assert "phase=BUFFER" in srv.fsm_to("BUFFER", pointer=1, mode="direct")
    assert srv.load()["mode"] == "direct"


@pytest.mark.parametrize("mutate", [
    lambda s: (s.PLAN / "01.md").write_text("tampered"),
    lambda s: (s.PLAN / "02.md").write_text(V),
    lambda s: (s.PLAN / "index.md").unlink(),
    lambda s: (s.PLAN / "summary.md").write_text("changed"),
], ids=["edit-step", "add-step", "delete-index", "edit-summary"])
def test_plan_is_immutable_after_draft(srv, mutate):
    assert "phase=BUFFER" in draft_plan(srv, [V])
    mutate(srv)
    assert "changed" in srv.run_implementor()
    assert srv.load()["phase"] == "BUFFER"


# ---------- VALIDATE / DONE / re-plan ----------
def test_validate_to_buffer_needs_pointer_direct_mode_directive_and_a_matching_verdict(srv, monkeypatch):
    reach_validate(srv, monkeypatch)
    assert srv.fsm_to("BUFFER").startswith("refused")
    assert srv.fsm_to("BUFFER", pointer=1, mode="progressive").startswith("refused")
    assert srv.fsm_to("BUFFER", pointer=1, mode="direct").startswith("refused")      # no directive.md
    srv.DIRECTIVE.write_text("STEP 1: fix it")
    assert srv.fsm_to("BUFFER", pointer=0, mode="direct").startswith("refused")
    assert srv.fsm_to("BUFFER", pointer=9, mode="direct").startswith("refused")
    assert "validate.md missing" in srv.fsm_to("BUFFER", pointer=1, mode="direct")
    write_validate(srv, "PASS")
    assert "verdict is PASS" in srv.fsm_to("BUFFER", pointer=1, mode="direct")
    write_validate(srv, "FAIL step=2")
    assert "need FAIL step=1" in srv.fsm_to("BUFFER", pointer=1, mode="direct")
    write_validate(srv, "FAIL step=1", run=0)
    assert "is for run 0" in srv.fsm_to("BUFFER", pointer=1, mode="direct")
    write_validate(srv, "FAIL step=1")
    assert "phase=BUFFER" in srv.fsm_to("BUFFER", pointer=1, mode="direct")
    s = srv.load()
    assert (s["mode"], s["needs_directive"], s["attempts"]) == ("direct", False, 0)
    monkeypatch.setattr(srv, "chat", FakeChat(*tool_pass()))          # and the fix round runs, back to VALIDATE
    assert "phase=VALIDATE" in srv.run_implementor()
    assert not srv.DIRECTIVE.exists() and srv.load()["run_id"] == 2


def test_done_then_new_plan_archives_old_plan(srv, monkeypatch):
    reach_validate(srv, monkeypatch)
    write_validate(srv, "PASS")
    assert "phase=DONE" in srv.fsm_to("DONE")
    assert srv.fsm_to("BUFFER", pointer=1, mode="direct").startswith("refused")      # DONE->BUFFER illegal
    assert "phase=PLAN" in srv.fsm_to("PLAN")
    archives = list((srv.AG / "archive").iterdir())
    assert len(archives) == 1 and (archives[0] / "01.md").exists()
    assert not srv.step_files() and not (srv.PLAN / "index.md").exists()
    assert (srv.PLAN / "summary.md").exists()
    s = srv.load()
    assert (s["total"], s["pointer"], s.get("plan_hash"), s["run_id"]) == (0, 1, None, 0)
    assert not srv.HANDOFF.exists() and not srv.PROGRESS.exists()     # a new plan starts with a clean slate
    assert (archives[0] / "handoff" / "implement.md").exists() and (archives[0] / "progress.md").exists()


def test_plan_defect_path_buffer_to_plan(srv):
    assert "phase=BUFFER" in draft_plan(srv, [V])
    assert "phase=PLAN" in srv.fsm_to("PLAN")
    assert len(list((srv.AG / "archive").iterdir())) == 1
    assert (srv.PLAN / "summary.md").exists()


def test_two_archives_in_the_same_second_do_not_collide(srv):
    """Found by the fuzz test: archive dirs were named per second, so a quick re-plan crashed."""
    srv.PLAN.mkdir(parents=True)
    (srv.PLAN / "01.md").write_text("x")
    srv.archive_plan()
    (srv.PLAN / "01.md").write_text("x")
    srv.archive_plan()
    assert len(list((srv.AG / "archive").iterdir())) == 2
