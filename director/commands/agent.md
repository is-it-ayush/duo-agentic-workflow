---
description: Start or resume the director workflow
---
Call fsm_status. If phase is NONE or DONE, call fsm_to('PLAN') and begin per director.md with this goal: $ARGUMENTS
Otherwise resume at the indicated phase.
