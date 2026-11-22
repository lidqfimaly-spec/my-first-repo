# Bounded Multi-Step Task Engine

Status: Implemented for structured local task plans

## Scope

The multi-step engine executes an explicit, bounded plan through existing
verified child tasks. A parent task records the graph and step evidence while
each child task retains its own Policy decision, lease, tool result,
verification, events, and artifact.

The engine does not make the LLM planner multi-step by default. It is exposed
through trusted local service APIs:

- `TaskService.submit_plan`
- `TaskService.request_pause`
- `TaskService.approve_step`
- `TaskService.replan_pending_steps`

## Plan contract

Each step has:

- a unique `step_id`;
- one supported tool operation;
- a bounded `parameters` object;
- an `expected_result`;
- zero or more explicit `depends_on` step IDs;
- optional `requires_approval`;
- `max_attempts` from 1 to 3;
- optional retry only when the prior attempt proved `not_executed`.

Plans contain at most eight steps. The final plan is acyclic. References to the
output of another step are explicit and require a declared dependency:

```json
{
  "step_id": "copy",
  "tool_name": "write_text_artifact",
  "depends_on": ["source"],
  "parameters": {
    "artifact_path": "copy.txt",
    "content": "${source.content}"
  }
}
```

No expression language or arbitrary template evaluation is used.

## Outcomes

- `succeeded`: every step completed and verified.
- `partial`: at least one step succeeded and another failed, was blocked, or
  was skipped. Partial is a terminal, explicitly incomplete outcome.
- `failed`: no step succeeded.
- `awaiting_approval`: an approval-gated step is next.
- `paused`: the plan stopped at a durable step boundary.

## Safety and recovery

- A child task cannot be marked successful from tool success alone.
- The parent persists a running step intent and preallocated child task ID
  before executing the child.
- After interruption, the same child task is recovered or re-executed through
  normal lease and verification rules; a new child is not created blindly.
- Pausing does not cancel a child already in progress. The parent stops at the
  documented boundary.
- Replanning is allowed only when paused or waiting for approval and only while
  no execution lease is active.
- Replanning preserves successful step evidence and rejects replacement of a
  completed step ID.
- Cancellation is checked before each step and before approval.

## Current limits

- Scheduling is sequential and deterministic. Dependency order is respected,
  but independent steps are not run in parallel yet.
- The LLM planner cannot emit `execute_plan` directly.
- Child tasks are independent task rows linked by `parent_task_id`; they are not
  hidden subtasks.
- A child that requests clarification does not currently propagate the
  clarification questions into the parent UI automatically.
- The control-center presentation of approval and resume actions is a later
  integration step.
