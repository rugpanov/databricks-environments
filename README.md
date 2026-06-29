# databricks-environments

Per-compute **dependency constraint artifacts** for Databricks runtimes. Each
supported environment (a DBR version or a serverless environment version) gets a
pinned `pyproject.toml` (for uv / Poetry) and `constraints.txt` (for pip / conda) so
developers can reproduce the runtime's Python environment locally — matching the
exact Python version, `databricks-connect` version, and transitive dependency set.

This is the source of truth consumed by the Databricks CLI / VS Code extension when
setting up a local environment for a selected compute target.

## Layout

```
python/
  serverless/
    serverless-v4/
      pyproject.toml
      constraints.txt
    serverless-v5/
      ...
  dbr/                      # (planned)
    17.3.x-cpu-ml-scala2.13/
      pyproject.toml
      constraints.txt
```

Top-level `python/` namespaces these as Python-ecosystem artifacts, leaving room for
other ecosystems later. Directory names mirror the identifiers the Databricks
platform exposes (`spark_version` for classic clusters, `serverless-vN` for
serverless), so resolving a target to its artifact is a deterministic lookup.

## Artifacts

- **`pyproject.toml`** (uv / Poetry) — `requires-python`, the `databricks-connect`
  pin in `[dependency-groups].dev` (installed by default under `uv sync`), and the
  full pinned set in `[tool.uv].constraint-dependencies`.
- **`constraints.txt`** (pip / conda) — flat `name~=version` pins, consumed via
  `PIP_CONSTRAINT` or `-c constraints.txt`. Does **not** list `databricks-connect`,
  so the pip path is constraints-only unless DB Connect is installed explicitly.

Both are a mechanical transform of the official package list published in the
Databricks release notes — see `scripts/envgen.py` for the rules.

## Keeping it in sync

`scripts/sync.py` discovers the published serverless environment versions from the
release notes, downloads each `requirements-env-N.txt`, regenerates both artifacts,
and reconciles them against what's committed:

```bash
python scripts/sync.py          # regenerate into the working tree
python scripts/sync.py --check  # report drift / new versions, exit non-zero if any
```

A scheduled GitHub Action (`.github/workflows/sync.yml`) runs this weekly and opens a
PR when an environment drifts or a new version appears. This docs-parsing sync is an
**interim** mechanism; the durable plan is for the runtime/environments build pipeline
to publish these files directly. See the design doc for the full rationale.

### Generating a single environment manually

```bash
python scripts/gen_pyproject.py requirements-env-4.txt serverless-v4 3.12.3 \
    python/serverless/serverless-v4
```

## Status

- [x] Serverless (v1–vN) — automated via docs sync
- [ ] DBR — release-notes pages list libraries inline in HTML; parser is a TODO
