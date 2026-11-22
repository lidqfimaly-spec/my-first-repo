# Zevorinth V1 Engine-Slice Acceptance

Status: Engine-slice acceptance runner; the V1 product user journey is not yet complete.

Date: 2026-11-10

Current measured engine-slice result: `PASS`

```text
acceptance_scope=ENGINE_SLICES
v1_user_journey_complete=false
```

| Metric | Result |
| --- | --- |
| Positive scenarios | 7/7 |
| Negative controls | 2/2 |
| False successes | 0 |
| Explicit interventions | 2 |
| Recovered tasks | 1 |
| Observed runtime | 450.189 ms |
| Mode | `REAL_LOCAL_TEMP_ISOLATED` |

The observed timing is from the current `CPython 3.14.6` run on
`macOS-27.0.1-arm64`. The authoritative JSON output for that run is
`/tmp/zevorinth-parentfix-accept.9yaSKD/acceptance.json`; a later run
writes new evidence rather than overwriting it.

## Purpose

The product acceptance runner executes representative V1 engine work through
the real local task runtime using isolated temporary homes and workspaces. It
does not accept planner- or task-reported success at face value: artifacts are
reopened, hashes are recomputed, task verification is checked, and negative
controls must fail closed.

This runner currently calls the trusted `TaskService` API directly. It does not
yet prove that every workflow can be originated and managed through the
control-center user journey.

## Command

```bash
PYTHONPATH=src python3 -m zevorinth.product_evaluation \
  --output /tmp/zevorinth-product-acceptance.json \
  --json
```

The runner writes deterministic-schema JSON and a Markdown summary. Existing
output paths are rejected to avoid silently replacing earlier evidence.

It is also available through:

```bash
PYTHONPATH=src python3 -m zevorinth \
  eval-product \
  --output /tmp/zevorinth-product-acceptance.json \
  --json
```

## Covered Scenarios

- File: create, independently read, and append into a new verified version
  without modifying the source.
- Code: inspect a registered workspace, pause for approval, apply one exact
  edit, run a host-authorized unittest, and verify the result.
- Research: generate a local-source report with source hashes, exact quotes,
  confidence labels, and explicit provenance limitations.
- Complex: execute dependent step data flow, report partial success, complete
  an approval-gated step, and recover an interrupted child without repeating
  its side effect.
- Negative controls: unauthorized code edits and fabricated research citations
  must be blocked before side effects.

## Metrics

- Positive scenario pass count and success rate.
- Negative-control pass count.
- False-success count when a task reports `succeeded` without independent
  artifact verification.
- Explicit human interventions.
- Recovered tasks.
- Per-task-class results.
- End-to-end latency.

## Boundaries

- The run executes only on the current macOS host.
- Code tests use the explicit `host` backend and require a one-time grant
  issued out of band (`zevorinth authorize-host-test`), bound to one
  workspace and target, single use, and never restored when a run fails.
- Research is local-source only and proves citation presence, not source truth.
- Code patch analysis and research synthesis are caller-authored in this slice.
- Multi-step pause, approval, resume and replan are not yet complete
  control-center user actions.
- Natural-language preview currently originates only bounded text-file
  creation.
- Network, live model, container, multi-user, multi-host, and production trust
  are not covered.
- A pass is evidence for the temporary execution described by its report, not a
  permanent or production certification.
