#!/usr/bin/env bash
# Tell a fresh Claude session that a workflow exists. Silent inside the headless implementor session.
[ -n "$AGENT_IMPL_RUN" ] && exit 0
d="${CLAUDE_PROJECT_DIR:-$PWD}"
[ -f "$d/.agent/state.json" ] && echo "Agent workflow active in $d. Call fsm_status, then follow ~/personal/agent/director/director.md."
exit 0
