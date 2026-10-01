#!/usr/bin/env bash
d="${CLAUDE_PROJECT_DIR:-$PWD}"
[ -f "$d/.agent/state.json" ] && echo "Agent workflow active in $d. Call fsm_status, then follow ~/personal/agent/director/director.md."
exit 0
