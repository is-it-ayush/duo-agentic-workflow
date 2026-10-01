# Protocol (code-enforced by the `agent` MCP server)
Phases: PLAN DRAFT BUFFER IMPLEMENT VALIDATE DONE. Only the server writes .agent/state.json.
Legal moves: PLAN>DRAFT, DRAFT>BUFFER, BUFFER>IMPLEMENT (run_implementor), IMPLEMENT>{IMPLEMENT,BUFFER,VALIDATE} (implementor only),
VALIDATE>{BUFFER,DONE}, DONE>PLAN, BUFFER>PLAN (plan defect only). Anything else is refused; do not try.
Control: you give it with run_implementor(); you get it back when that call returns one HANDOFF line.
Plan files (.agent/plan) are immutable after DRAFT>BUFFER.
Modes: progressive (implementor runs all remaining steps) | direct (implementor runs only `pointer`, then VALIDATE).
