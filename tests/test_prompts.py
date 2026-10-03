"""Drift checks: prompt/agent files vs what server.py actually enforces. Static, no processes."""
import re
import pytest
from helpers import step_text

SUBAGENTS = ["drafter", "unblocker", "validator"]
KNOWN_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebSearch", "WebFetch", "Task"}


def read(srv, rel):
    return (srv.HOME / rel).read_text()


def frontmatter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "missing --- frontmatter"
    return {k.strip(): v.strip() for k, v in (l.split(":", 1) for l in m.group(1).splitlines() if ":" in l)}


def test_required_files_exist(srv):
    for rel in ("common/style.md", "common/protocol.md", "common/discussion.md", "director/director.md",
                "director/commands/agent.md", "implementor/implementor.md",
                *[f"director/agents/{n}.md" for n in SUBAGENTS]):
        assert (srv.HOME / rel).is_file(), rel


@pytest.mark.parametrize("name", SUBAGENTS)
def test_subagent_frontmatter(srv, name):
    fm = frontmatter(read(srv, f"director/agents/{name}.md"))
    assert fm["name"] == name and fm["description"] and fm.get("model")
    tools = {t.strip() for t in fm["tools"].split(",")}
    assert tools and tools <= KNOWN_TOOLS, tools - KNOWN_TOOLS


def test_slash_command_has_description(srv):
    assert frontmatter(read(srv, "director/commands/agent.md"))["description"]


def test_director_only_requests_legal_director_transitions(srv):
    text = read(srv, "director/director.md")
    legal = {t for (f, t), a in srv.EDGES.items() if a == "director"} - {"IMPLEMENT"}
    used = set(re.findall(r"fsm_to\('(\w+)'", text))
    assert used and used <= legal, f"director.md uses illegal targets: {used - legal}"
    assert "run_implementor" in text and "user_guided" in text


def test_every_subagent_is_referenced_and_exists(srv):
    on_disk = {p.stem for p in (srv.HOME / "director/agents").glob("*.md")}
    in_director = set(re.findall(r"spawn `(\w+)`", read(srv, "director/director.md")))
    in_next = set(re.findall(r"spawn (?:subagent )?'(\w+)'", " ".join(srv.NEXT.values())))
    assert on_disk == set(SUBAGENTS) == in_director == in_next


def test_drafter_template_is_accepted_by_the_server(srv):
    text = read(srv, "director/agents/drafter.md")
    m = re.search(r"^GOAL:.*?^LOCK:[^\n]*", text, re.S | re.M)
    assert m, "drafter.md has no step template"
    assert str(srv.STEP_CHAR_CAP) in text, "drafter.md must state the server's size cap"
    srv.PLAN.mkdir(parents=True)
    (srv.PLAN / "index.md").write_text("01 t")
    (srv.PLAN / "01.md").write_text(m.group(0) + "\n")
    assert srv.check_plan() is None


def test_style_declares_every_message_type_used_in_prompts(srv):
    style = read(srv, "common/style.md")
    for tag in ("QUESTION", "PUSHBACK", "STATUS", "SUMMARY", "REPORT", "CHECKPOINT", "DIRECTIVE"):
        assert tag in style


def test_implementor_prompt_mentions_every_tool_it_is_given(srv):
    text = read(srv, "implementor/implementor.md")
    missing = [n for n in srv.make_tools() if n not in text]
    assert not missing, f"implementor.md never mentions: {missing}"


def test_style_does_not_forbid_the_tools_the_implementor_has(srv):
    style = read(srv, "common/style.md")
    assert "Implementor never" not in style, "style.md still forbids destructive ops; delete_path now exists"


def test_drafter_handoff_example_is_accepted_by_the_server(srv):
    text = read(srv, "director/agents/drafter.md")
    assert "handoff/draft.md" in text and "DRAFT WRITTEN: <N> steps" in text
    srv.HANDOFF.mkdir(parents=True)
    (srv.HANDOFF / "draft.md").write_text("DRAFT WRITTEN: <N> steps".replace("<N>", "2") + "\n")
    assert srv.check_draft_handoff(2) is None


def test_validator_verdict_examples_are_accepted_by_the_server(srv):
    text = read(srv, "director/agents/validator.md")
    assert "handoff/validate.md" in text and "handoff/implement.md" in text
    srv.HANDOFF.mkdir(parents=True)
    for example in ("PASS run=<k>", "FAIL step=<n> run=<k>"):
        assert example in text, example
        (srv.HANDOFF / "validate.md").write_text(example.replace("<k>", "3").replace("<n>", "2") + "\n")
        verdict, why = srv.validate_verdict({"run_id": 3})
        assert why is None and verdict in (("PASS", None), ("FAIL", 2))


def test_unblocker_reads_the_implementor_handoff(srv):
    assert "handoff/implement.md" in read(srv, "director/agents/unblocker.md")


def test_director_prompt_follows_the_enforced_handoffs(srv):
    text = read(srv, "director/director.md")
    assert "validate.md" in text and "draft.md" in text and "/clear" in text
    assert "AGENT_MODEL" in text and "Never spawn an implementor" in text


def test_director_prompt_keeps_every_recovery_path(srv):
    """A hand-edit of director.md once dropped the unblocker, plan-defect and halt instructions."""
    text = read(srv, "director/director.md")
    for needle in ("needs_directive", "spawn `unblocker`", "PLAN DEFECT", "halted=true", "user_guided=true",
                   "spawn `validator`", "spawn `drafter`", "fsm_to('DONE')", "fsm_to('BUFFER', pointer=<n>, mode='direct')"):
        assert needle in text, f"director.md lost: {needle}"
