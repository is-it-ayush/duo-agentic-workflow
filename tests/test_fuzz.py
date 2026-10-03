"""Model-based fuzz: random director/implementor operations must never break FSM invariants."""
import random
import pytest
from helpers import PHASES, FakeChat, call, reply, step_text, write_draft_handoff, write_validate

TARGETS = ["PLAN", "DRAFT", "BUFFER", "IMPLEMENT", "VALIDATE", "DONE"]


def chat_for(kind):
    if kind == "pass":
        return FakeChat(default=reply(call("write_file", path="ok.txt", content="x"),
                                      call("finish_step"), content="PROBLEM: p"))
    if kind == "block":
        return FakeChat(default=reply(call("blocked", reason="r"), content="PROBLEM: p"))
    return FakeChat(default=reply(call("finish_step"), content="PROBLEM: p"))


@pytest.mark.parametrize("seed", range(40))
def test_random_walk_keeps_fsm_invariants(srv, monkeypatch, seed):
    rnd = random.Random(seed)
    log = []
    real_move = srv.move

    def spy(s, actor, to, **upd):
        frm = s["phase"]
        real_move(s, actor, to, **upd)
        log.append((frm, to, actor))
    monkeypatch.setattr(srv, "move", spy)
    srv.PLAN.mkdir(parents=True, exist_ok=True)

    def op_to():
        return srv.fsm_to(rnd.choice(TARGETS), pointer=rnd.choice([0, 1, 2, 5]),
                          mode=rnd.choice(["", "progressive", "direct"]))

    def op_summary():
        (srv.PLAN / "summary.md").write_text("s")

    def op_plan(good=None):
        good = rnd.random() < 0.8 if good is None else good
        srv.PLAN.mkdir(parents=True, exist_ok=True)
        for p in srv.step_files():
            p.unlink()
        (srv.PLAN / "index.md").write_text("i")
        n = rnd.choice([1, 2, 3])
        for i in range(1, n + 1):
            (srv.PLAN / f"{i:02d}.md").write_text(step_text("test -f ok.txt"))
        if good:
            write_draft_handoff(srv, n)
        elif rnd.random() < 0.5:
            write_draft_handoff(srv, n + 1)                  # wrong count

    def op_validate():
        run = srv.load().get("run_id", 0)
        write_validate(srv, rnd.choice(["PASS", "FAIL step=1", "FAIL step=2"]), run=rnd.choice([run, run, max(run - 1, 0)]))

    def op_directive():
        srv.DIRECTIVE.write_text("DO: x")

    def op_tamper():
        (srv.PLAN / "index.md").write_text(str(rnd.random()))

    def op_run():
        kind = rnd.choice(["pass", "pass", "block", "fail"])
        if kind != "pass":
            (srv.ROOT / "ok.txt").unlink(missing_ok=True)
        monkeypatch.setattr(srv, "chat", chat_for(kind))
        return srv.run_implementor(user_guided=rnd.random() < 0.5)

    def op_advance():
        """The next sensible director move for the current phase; 15% of the time it skips a prerequisite."""
        ph, noisy = srv.load()["phase"], rnd.random() < 0.15
        if ph in ("NONE", "DONE"):
            return srv.fsm_to("PLAN")
        if ph == "PLAN":
            if not noisy:
                op_summary()
            return srv.fsm_to("DRAFT")
        if ph == "DRAFT":
            op_plan(good=not noisy)
            return srv.fsm_to("BUFFER", pointer=rnd.choice([0, 0, 1]), mode=rnd.choice(["", "progressive", "direct"]))
        if ph == "BUFFER":
            if srv.load().get("needs_directive") and not noisy:
                op_directive()
            return op_run()
        if ph == "VALIDATE":
            verdict = rnd.choice(["PASS", "FAIL step=1"])
            if not noisy:
                write_validate(srv, verdict)
            if verdict == "PASS":
                return srv.fsm_to("DONE")
            op_directive()
            return srv.fsm_to("BUFFER", pointer=1, mode="direct")

    ops = [op_advance, op_advance, op_advance, op_advance, op_to, op_to, op_summary, op_plan, op_plan, op_directive, op_tamper,
           op_run, op_run, op_run, op_validate, op_validate, srv.fsm_status]

    for _ in range(60):
        before = srv.load()["phase"]
        log.clear()
        rnd.choice(ops)()
        s = srv.load()
        after = s["phase"]
        assert after in PHASES
        assert after != "IMPLEMENT", "control must be handed back before any call returns"
        assert srv.RUNNING is False
        cur = before                                    # every phase change goes through move(), in order
        for frm, to, actor in log:
            assert frm == cur, (before, log)
            assert srv.EDGES.get((frm, to)) == actor
            cur = to
        assert cur == after, (before, after, log)
        if before != "NONE":
            assert after != "NONE"
        assert s.get("attempts", 0) <= srv.MAX_ATTEMPTS
        if after in ("BUFFER", "VALIDATE", "DONE") and s.get("total"):
            assert 1 <= s["pointer"] <= s["total"] + 1
