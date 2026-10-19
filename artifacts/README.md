# Artifacts

`artifacts/` is reserved for files produced by real tool executions.

- Each task gets an isolated subdirectory named by its task ID.
- Runtime artifacts are ignored by Git.
- Artifact metadata and verification evidence belong in the task event log.
- An artifact is not considered verified merely because a file exists; it must pass a declared verifier.
- No credentials, cookies, tokens, private keys, or unrelated user files may be stored here.
