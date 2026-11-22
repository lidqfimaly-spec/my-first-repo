# Controlled Code Task Workflow

Status: Implemented for registered local workspaces

## Scope

The first code-task slice exposes two trusted structured operations:

- `inspect_workspace`: bounded read-only inspection.
- `edit_text_file`: one authorized exact-text replacement followed by
  independent readback verification.

Neither operation is selectable directly by the LLM planner. A local caller or
the multi-step service constructs the explicit operation.

The preferred service entry point is:

```python
task = service.submit_code_task(
    "update the implementation and verify it",
    workspace="demo",
    path="sample.py",
    old_text="VALUE = 1",
    new_text="VALUE = 2",
    test_target="test_sample",
)
```

This creates an approval-gated plan:

```text
inspect -> approve exact workspace edit -> edit -> run approved tests
```

## Workspace inspection

Inspection is confined to `workspaces/<workspace>` under the task runtime home.
It rejects absolute paths, traversal, symbolic links, `.idea`, `.git`, virtual
environments, dependency trees, caches, `.env*`, common credential files, and
sensitive `secret`/`credentials` path components.

Limits:

- at most 200 files per operation;
- at most 512 KiB read from one file;
- at most 4 MiB read in total;
- a single bounded query string for deterministic matching.

The inspection is repeated by an independent verifier. Any content, path,
digest, truncation, or file-count change fails verification.

## Exact text edit

The caller must provide:

- registered `workspace`;
- canonical relative `path`;
- exact source `expected_sha256`;
- non-empty `old_text`;
- `new_text`.

The trusted control center injects `local-code-edit` only after the user
confirms the structured edit request. The browser cannot choose or override
that authorization value. Direct trusted service callers must provide it
themselves.

The edit is rejected before side effects when:

- authorization is absent or wrong;
- the path escapes the registered workspace;
- the file is a symbolic link, binary, invalid UTF-8, or over 1 MiB;
- the source digest does not match;
- `old_text` matches zero times or more than once;
- the resulting file exceeds 1 MiB.

The original bytes are written to a task-scoped backup under
`artifacts/<task_id>/workspace-backups/`. The new bytes are written to a
same-directory temporary file, flushed, atomically replaced, and independently
read back. Success requires the observed bytes and SHA-256 to match the intended
result and source lineage.

## Recovery

The edit intent and expected result digest are persisted before the side effect.
If the process stops after replacement but before verification:

- a matching result digest permits recovery by verification only;
- the edit is not applied a second time;
- an unprovable or conflicting file state fails closed as unknown.

## Remaining limits

- The workflow is text replacement, not a general code editor.
- It does not provide arbitrary shell execution.
- The current multi-step scheduler is sequential.
- The control center does not yet expose code-task approval controls.
- Container-backed test evidence remains environment-dependent.
