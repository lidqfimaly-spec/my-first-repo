# Runtime Data

`data/` is reserved for durable, non-secret runtime state.

Planned Phase 1 layout:

```text
data/
  tasks/
    <task-id>/
      state.json
      events.jsonl
```

Persistent data:

- task identity and bounded goal;
- state-machine status;
- plan revision and approval state;
- tool request/result summaries;
- verification evidence and artifact references;
- clarification and interruption metadata.

Temporary data:

- in-memory planning candidates;
- transient provider responses that are not evidence;
- test fixtures created under the system temporary directory.

Secrets must be supplied through environment variables or a future OS keychain integration. They must never be written to `data/`, `artifacts/`, event logs, or test output.
