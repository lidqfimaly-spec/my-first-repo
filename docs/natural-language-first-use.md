# Natural-Language First Use

Status: Implemented intent routing for the local control center's supported V1
task classes.

## Purpose

The first-use flow lets a local user describe a task in natural language without
knowing operation names, task identifiers, leases, or event formats. The system
interprets the goal, displays editable parameters and visible assumptions, and
requires a separate explicit confirmation before creating a task.

The preview is not an execution authorization. It does not create a task,
claim a lease, invoke a tool, or publish a result.

## Endpoint

`POST /control/api/preview`

The endpoint requires the same loopback Host, matching Origin, and per-process
control token as the other control-center POST routes.

Request:

```json
{"goal": "创建 `hello.txt`，内容写：Hello from Zevorinth"}
```

Successful preview:

```json
{
  "contract_version": "task-control-v1",
  "preview_version": "natural-goal-preview-v1",
  "state": "ready",
  "operation": "write_text_artifact",
  "parameters": {
    "artifact_path": "hello.txt",
    "content": "Hello from Zevorinth"
  },
  "missing_fields": [],
  "confirmation_required": true,
  "execution_started": false,
  "mutation_started": false
}
```

The response also includes a human-readable operation label, assumptions,
planned verification outcomes, and current limits.

## Preview states

| State | Meaning |
| --- | --- |
| `ready` | The bounded file-create request is complete and can be edited before confirmation |
| `needs_clarification` | A safe task intent was recognized, but required user-controlled fields remain missing |
| `unsupported` | The goal cannot be mapped to the currently enabled natural-language flow |

Unsupported, ambiguous or unsafe goals fail closed. Clear code, local-research
and complex-plan intents route to their guided clarification forms rather than
inventing missing fields or executing directly.

## User journey

1. Enter a natural-language goal.
2. Select `理解目标并生成预览`.
3. Inspect the interpreted goal, operation label, parameters, assumptions,
   missing fields, plan verification outcomes, and limits.
4. Edit the displayed file name or content if needed.
5. Confirm that creation is not execution.
6. Create the durable task and, when eligible, explicitly start it through the
   existing control-center action path.
7. Read the authoritative task status, verification evidence, and artifact.

## Boundaries

- File creation may become immediately ready after preview. Code, research and
  complex plans require the corresponding guided fields and explicit
  confirmation.
- The task remains subject to the existing deterministic planner, Policy,
  lease, artifact writer, and independent verifier.
- No arbitrary file path, shell command, browser action, or test start is
  enabled by this flow.
- The current interpreter is deterministic and intentionally narrow. A future
  LLM-backed preview may replace the interpretation layer only if the same
  preview/confirmation boundary is preserved.

## Advanced structured task mode

The control center also exposes an explicit **高级结构化任务** mode for
operations that cannot yet be inferred safely from a short natural-language
sentence. It uses the same creation endpoint and shows the operation and JSON
parameters before confirmation.

The mode can create verified artifact reads and versioned modifications,
registered-workspace inspection, authorized exact-text edits, local
source-preserving research reports, and bounded `execute_plan` graphs with up
to eight steps.

Advanced mode is not silent inference. Invalid JSON, unsupported operations,
unsafe paths, missing trusted fields, and Policy rejections fail closed before
execution. The user confirms creation separately and starts eligible queued
tasks through a version-checked control action.

## Multi-step task controls

Task detail now exposes server-backed pause, resume, approve-step and
reject-step actions for `execute_plan` tasks. Pending-step replanning is
available through a bounded JSON step array. All actions require the loopback
Origin, per-process token, current task version and explicit confirmation.

A pause requested while a running step owns the lease records only the pause
intent; it becomes `paused` at a safe step boundary. Approval decisions are
bound to the exact pending step. Replanning preserves completed step evidence
and replaces only unfinished work.
