# Offline Source-Preserving Research Workflow

Status: available as a trusted structured operation and bounded plan step.

## Purpose

`research_report` turns a bounded set of local text sources into a deterministic
Markdown report. It preserves source identity, exact citation text, uncertainty
labels, and an explicit limitation that local consistency does not establish
source authenticity.

Network and browser collection remain disabled.

## Inputs

- `workspace`: an already registered directory below `workspaces/`;
- `sources`: 1-20 canonical relative UTF-8 text paths;
- `title`: a non-empty single-line report title;
- `output_path`: a non-overwriting `.md` file in the task artifact directory;
- `findings`: 1-40 structured findings. Every finding has:
  - `statement`;
  - `confidence`, one of `supported`, `inference`, or `uncertain`;
  - 1-8 citations, each with a declared `source_path` and an exact `quote`.

Each quote must occur in the recorded UTF-8 source bytes. Undeclared sources,
duplicate sources, sensitive paths, symlinks, traversal, `.idea`, `.git`,
`.env*`, caches, virtual environments, and credential-like files are rejected.

## Execution

1. Validate all paths and structured fields.
2. Read bounded source bytes and compute SHA-256.
3. Verify every citation quote against the exact source text.
4. Render deterministic Markdown with source identities, hashes, findings,
   confidence labels, quotes, collection time, and limitations.
5. Write through the existing non-overwriting artifact publisher.
6. Independently reread the report.
7. Rehash all sources, revalidate every quote, and compare the report and
   source-lineage evidence.

Only after all checks pass can the task reach `succeeded`.

## Recovery

If execution stops after report publication but before durable tool evidence is
recorded, recovery reconstructs the deterministic report from the original
task time and source inputs. It verifies the published bytes without rewriting
them. A source change, mismatched report, or unavailable source fails closed.

## Limits

- This is local source organization and citation verification, not unrestricted
  internet research.
- A verified quote proves occurrence in the recorded bytes only.
- It does not prove the source is true, current, complete, or authentic.
- Findings supplied by an upstream model remain unverified unless their exact
  citations match local source bytes.
