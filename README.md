# AIME Event Wiki

Evidence-first, temporally safe agents that compile cleaned financial content into a
versioned Event Wiki. The project is intentionally separated from the private source data:
the repository contains code and synthetic fixtures only.

## Safety boundary

Do not commit source JSONL/Parquet files, generated Wiki pages, model outputs, credentials,
or excerpts copied from the private corpus. Public fixtures must be synthetic.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
docker compose up -d postgres
.venv/bin/alembic upgrade head
.venv/bin/event-wiki --help
```

The default data root is `../recent_one_year_data`. Ingestion is read-only.

```bash
event-wiki ingest --month 2025-11
event-wiki candidates
event-wiki run --limit 20
event-wiki serve
```

Review patches at `http://127.0.0.1:8000/reviews`. Approved versions can be exported with:

```bash
event-wiki export markdown
event-wiki export structured --cutoff 2025-12-01T00:00:00Z
event-wiki export audit-report
event-wiki export graph
```

The review server is local-only by default. Set `REVIEW_TOKEN` (or pass
`--review-token`) before binding it to a non-loopback address.

## Graph model

The system deliberately uses two graphs. LangGraph is the resumable execution graph for
discovery, resolution, extraction, audit, review, and commit. PostgreSQL stores the temporal
knowledge graph: versioned event, entity, claim, relation, and evidence nodes connected by
evidence-backed edges. `known_at` cutoffs produce historical graph snapshots without leaking
later information.

## Verification

```bash
ruff check .
ruff format --check .
pytest -q
event-wiki security-scan
```

The test suite includes a fully synthetic 200-document/30-event gold set, graph resume/rerun,
optimistic locking, temporal leakage, review conflicts, exports, and scale regression tests.

## MVP boundaries

`merge_event` and `supersede_claim` remain fail-closed until their explicit mutation contracts
are introduced; agents cannot silently approximate either operation. A real one-month trial is
an operational release step because it requires local source ingestion, model credentials, and
human review. Its generated data and metrics must remain under `.local/` and outside Git.

## Status

This repository implements the local MVP and requires human approval for every Wiki patch.
It has no license; all rights are reserved unless a license is added later.
