"""Model-based fuzz: random director/implementor operations must never break FSM invariants."""
import random
import pytest
from helpers import PHASES, FakeChat, call, reply, step_text

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

    def op_plan():
        for p in srv.step_files():
            p.unlink()
        (srv.PLAN / "index.md").write_text("i")
        for i in range(1, rnd.choice([1, 2, 3]) + 1):
            (srv.PLAN / f"{i:02d}.md").write_text(step_text("test -f ok.txt"))

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

    ops = [op_to, op_to, op_summary, op_plan, op_plan, op_directive, op_tamper,
           op_run, op_run, op_run, srv.fsm_status]

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
