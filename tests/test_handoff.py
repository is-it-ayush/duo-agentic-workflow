"""Stage-to-stage handoffs: each stage leaves a summary the server checks, and the next stage starts from it."""
import pytest
from helpers import (FakeChat, call, draft_plan, reach_validate, reply, step_text, tool_pass,
                     write_draft_handoff, write_validate)


def steps(n):
    return [step_text(f"test -f a{i}.txt", goal=f"goal {i}") for i in range(1, n + 1)]


def run_steps(srv, monkeypatch, n):
    assert "phase=BUFFER" in draft_plan(srv, steps(n))
    fake = FakeChat(*[r for i in range(1, n + 1) for r in tool_pass(f"a{i}.txt")])
    monkeypatch.setattr(srv, "chat", fake)
    assert "phase=VALIDATE" in srv.run_implementor()
    return fake


# ---------- DRAFT -> BUFFER needs the drafter's summary ----------
@pytest.mark.parametrize("content,msg", [
    (None, "handoff/draft.md missing"),
    ("", "must start with"),
    ("done drafting\n", "must start with"),
    ("DRAFT WRITTEN: 2 steps\n", "says 2 steps but the plan has 1"),
    ("DRAFT WRITTEN: 1 steps\n" + "x" * 3000, "too long"),
], ids=["missing", "empty", "wrong-first-line", "wrong-count", "too-long"])
def test_buffer_is_refused_without_a_valid_drafter_handoff(srv, content, msg):
    out = draft_plan(srv, steps(1), handoff=False)
    assert out.startswith("refused") and "handoff/draft.md" in out
    if content is not None:
        srv.HANDOFF.mkdir(parents=True, exist_ok=True)
        (srv.HANDOFF / "draft.md").write_text(content)
        out = srv.fsm_to("BUFFER")
        assert out.startswith("refused") and msg in out, out
    assert srv.load()["phase"] == "DRAFT"
    write_draft_handoff(srv, 1)
    assert "phase=BUFFER" in srv.fsm_to("BUFFER")


# ---------- VALIDATE -> * must follow the validator's verdict for THIS run ----------
@pytest.mark.parametrize("content,msg", [
    (None, "validate.md missing"),
    ("all good\n", "must start with"),
    ("PASS\n", "must start with"),
    ("PASS run=0\n", "is for run 0"),
    ("FAIL step=1 run=1\n", "validator said FAIL step=1"),
], ids=["missing", "free-text", "no-run-id", "stale-run", "fail-verdict"])
def test_done_requires_a_passing_verdict_for_the_current_run(srv, monkeypatch, content, msg):
    reach_validate(srv, monkeypatch)
    if content is not None:
        srv.HANDOFF.mkdir(parents=True, exist_ok=True)
        (srv.HANDOFF / "validate.md").write_text(content)
    out = srv.fsm_to("DONE")
    assert out.startswith("refused") and msg in out, out
    assert srv.load()["phase"] == "VALIDATE"
    write_validate(srv, "PASS")
    assert "phase=DONE" in srv.fsm_to("DONE")


def test_a_verdict_from_an_earlier_run_goes_stale(srv, monkeypatch):
    reach_validate(srv, monkeypatch)
    write_validate(srv, "FAIL step=1")                                   # run 1 verdict
    srv.DIRECTIVE.write_text("STEP 1: fix")
    assert "phase=BUFFER" in srv.fsm_to("BUFFER", pointer=1, mode="direct")
    monkeypatch.setattr(srv, "chat", FakeChat(*tool_pass()))
    assert "phase=VALIDATE" in srv.run_implementor()                     # run 2
    out = srv.fsm_to("DONE")                                             # the old FAIL (or any run-1 file) cannot be reused
    assert out.startswith("refused") and "is for run 1, the current run is 2" in out


# ---------- the implementor stage's own summary ----------
def test_implement_handoff_summarises_the_run_for_the_next_stage(srv, monkeypatch):
    run_steps(srv, monkeypatch, 2)
    text = (srv.HANDOFF / "implement.md").read_text()
    assert text.startswith("RUN 1\n") and "outcome: all 2 steps passed" in text
    assert "- step 1: goal 1 | files: a1.txt" in text and "- step 2: goal 2 | files: a2.txt" in text


def test_escalation_handoff_points_at_the_checkpoint(srv, monkeypatch):
    assert "phase=BUFFER" in draft_plan(srv, [step_text("false")])
    monkeypatch.setattr(srv, "chat", FakeChat(default=reply(call("blocked", reason="r"))))
    assert "phase=BUFFER" in srv.run_implementor()
    assert "escalated at step 1" in (srv.HANDOFF / "implement.md").read_text()


# ---------- the next stage's fresh context starts from the previous stage's summary ----------
def test_each_step_is_told_what_the_earlier_steps_did(srv, monkeypatch):
    fake = run_steps(srv, monkeypatch, 3)
    one, two, three = fake.first_user_messages()
    assert "PREVIOUS STEPS" not in one
    assert "PREVIOUS STEPS (already done, do not redo):\n- step 1: goal 1 | files: a1.txt | commit: " in two
    assert "step 2:" not in two and "step 1: goal 1" in three and "step 2: goal 2" in three


def test_only_the_last_five_steps_are_passed_on(srv, monkeypatch):
    fake = run_steps(srv, monkeypatch, 7)
    last = fake.first_user_messages()[-1]
    assert "step 1:" not in last and all(f"step {i}: goal {i}" in last for i in range(2, 7))


def test_a_fix_round_sees_the_history_and_the_directive(srv, monkeypatch):
    run_steps(srv, monkeypatch, 2)
    write_validate(srv, "FAIL step=1")
    srv.DIRECTIVE.write_text("STEP 1: do better")
    assert "phase=BUFFER" in srv.fsm_to("BUFFER", pointer=1, mode="direct")
    fake = FakeChat(*tool_pass("a1.txt"))
    monkeypatch.setattr(srv, "chat", fake)
    assert "phase=VALIDATE" in srv.run_implementor()
    task = fake.first_user_messages()[0]
    assert task.startswith("STEP 1/2") and "- step 2: goal 2" in task and "DIRECTIVE:\nSTEP 1: do better" in task
    assert "- fix step 1: goal 1" in srv.PROGRESS.read_text()


def test_a_new_plan_starts_without_the_old_runs_summaries(srv, monkeypatch):
    run_steps(srv, monkeypatch, 1)
    assert srv.PROGRESS.exists()
    write_validate(srv, "PASS")
    assert "phase=DONE" in srv.fsm_to("DONE")
    assert "phase=PLAN" in srv.fsm_to("PLAN")
    assert not srv.PROGRESS.exists() and not srv.HANDOFF.exists()
