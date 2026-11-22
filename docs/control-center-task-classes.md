# Control Center Task Classes

Status: Implemented local product surface for the current engine slices

Date: 2026-11-10

## Guided and advanced creation

The control center starts with a natural-language goal. Clear text-file, code,
local-research and explicit complex-plan intents route into the appropriate
guided form. The route only selects and explains fields; it never invents
missing workspace, digest, patch, source, citation or plan data.

- verified artifact read and versioned modification;
- registered workspace inspection;
- authorized exact-text code edit;
- source-preserving local research report;
- bounded multi-step `execute_plan` through an explicitly labeled debug JSON
  fallback.

Code and research use ordinary fields rather than requiring users to author
JSON. Research findings use one bounded row per finding:
`supported|source-path|exact quote|statement`. Raw JSON remains available only
as an explicit debug/fallback mode. Every form still requires explicit
confirmation and cannot bypass the deterministic planner, Policy, workspace
boundaries, leases or independent verification.

For code edits, the server injects the exact-edit authorization value only
after the confirmed structured request is accepted. A browser-supplied
authorization value is ignored and replaced by server-side policy.

## Multi-step actions

`execute_plan` task detail exposes:

- pause request;
- resume;
- approve or reject the exact pending step;
- replace unfinished steps through a bounded JSON plan array.

Running-step pause is cooperative and takes effect only at a safe step boundary.
Every HTTP action requires loopback Host, matching Origin, process token,
current task version and explicit confirmation. Repeated action IDs use the
existing control receipt path.

Pause is advertised only when it can make progress immediately: a running
cooperative request may be shown while its active lease is owned by the
executor, and queued or approval-waiting pause is shown only when no lease must
first be recovered.

## Artifact delivery

A successful multi-step file task publishes a verified parent-owned final
artifact. The existing task detail and artifact preview endpoints therefore
show the final delivery without exposing child evidence as parent-owned data.
Step results remain available for intermediate evidence.

## Remaining limits

- Complex-plan construction still requires the bounded structured plan
  builder/debug fallback after intent routing.
- Proposal candidates can carry code patches or research findings and are
  deterministically validated before materialization. Live model providers are
  disabled by default, so general live synthesis quality remains unverified.
- Multi-step scheduling is deterministic and sequential.
- No network research, arbitrary shell, remote multi-user access or production
  trust is enabled.
