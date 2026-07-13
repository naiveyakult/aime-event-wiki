from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from event_wiki.db import Repository
from event_wiki.export import (
    export_audit_report,
    export_graph_json,
    export_markdown,
    export_structured,
    export_structured_jsonl,
)
from event_wiki.models import AuditResult, Claim, EvidenceDocument, WikiPatch


class FakeExportRepository:
    def list_export_events(self) -> list[dict]:
        return [
            {
                "event_id": "EVENT_1",
                "title": "Synthetic earnings",
                "event_family": "earnings",
                "event_subject": "Example Corp",
                "event_time": "2025-11-03T21:00:00Z",
                "known_at": "2025-11-03T21:00:00Z",
                "status": "approved",
                "version": 2,
                "audit": {"status": "PASS", "issues": []},
                "claims": [
                    {
                        "claim_id": "CLAIM_1",
                        "subject": "Example Corp",
                        "predicate": "reported_revenue",
                        "object_value": "100",
                        "known_at": "2025-11-03T21:00:00Z",
                        "evidence_ids": ["DOC_1"],
                    },
                    {
                        "claim_id": "CLAIM_FUTURE",
                        "subject": "Example Corp",
                        "predicate": "raised_guidance",
                        "object_value": "later",
                        "known_at": "2025-11-05T21:00:00Z",
                        "evidence_ids": ["DOC_2"],
                    },
                ],
                "relations": [
                    {
                        "relation_id": "REL_1",
                        "source_entity": "Example Corp",
                        "target_entity": "Synthetic Partner",
                        "relation_type": "partner",
                        "known_at": "2025-11-02T21:00:00Z",
                        "evidence_ids": ["DOC_1"],
                    }
                ],
                "evidence": [
                    {"evidence_id": "DOC_1", "title": "Synthetic filing"},
                    {"evidence_id": "DOC_2", "title": "Later report"},
                ],
                "review_actions": [{"action": "approve", "reviewer": "tester"}],
            },
            {
                "event_id": "EVENT_BLOCKED",
                "title": "Blocked",
                "known_at": "2025-11-03T21:00:00Z",
                "status": "approved",
                "audit": {"status": "BLOCK", "issues": [{"code": "future_information"}]},
            },
            {
                "event_id": "EVENT_PENDING",
                "title": "Pending",
                "known_at": "2025-11-03T21:00:00Z",
                "status": "pending",
                "audit": {"status": "PASS", "issues": []},
            },
        ]


CUTOFF = datetime(2025, 11, 4, tzinfo=UTC)


def test_structured_and_markdown_exports_filter_status_audit_and_cutoff(tmp_path: Path) -> None:
    repository = FakeExportRepository()
    structured_path = tmp_path / "events.jsonl"

    assert export_structured(repository, structured_path, cutoff=CUTOFF) == 1
    record = json.loads(structured_path.read_text())
    assert record["event_id"] == "EVENT_1"
    assert [claim["claim_id"] for claim in record["claims"]] == ["CLAIM_1"]
    assert {item["evidence_id"] for item in record["evidence"]} == {"DOC_1"}

    paths = export_markdown(repository, tmp_path / "wiki", cutoff=CUTOFF)
    assert len(paths) == 1
    assert "Synthetic earnings" in paths[0].read_text()
    assert "CLAIM_FUTURE" not in paths[0].read_text()


def test_audit_and_graph_exports(tmp_path: Path) -> None:
    repository = FakeExportRepository()
    audit_path = tmp_path / "audit.json"
    graph_path = tmp_path / "graph.json"

    export_audit_report(repository, audit_path)
    report = json.loads(audit_path.read_text())
    assert report["summary"]["blocked"] == 1
    assert report["summary"]["approved_exportable"] == 1

    export_graph_json(repository, graph_path, cutoff=CUTOFF)
    graph = json.loads(graph_path.read_text())
    nodes = {(node["type"], node["id"]) for node in graph["nodes"]}
    assert ("event", "EVENT_1") in nodes
    assert ("entity", "Example Corp") in nodes
    assert ("entity", "Synthetic Partner") in nodes
    assert ("evidence", "DOC_1") in nodes
    assert ("claim", "CLAIM_1") in nodes
    assert ("claim", "CLAIM_FUTURE") not in nodes
    assert any(edge["type"] == "partner" for edge in graph["edges"])
    assert any(edge["type"] == "supported_by" for edge in graph["edges"])


def test_real_sqlite_repository_exports_nested_version_snapshot(tmp_path: Path) -> None:
    repository = Repository.from_url(f"sqlite:///{tmp_path / 'wiki.db'}", create_schema=True)
    known_at = datetime(2025, 11, 3, 21, tzinfo=UTC)
    repository.upsert_evidence(
        EvidenceDocument(
            evidence_id="DOC_SQLITE",
            content_type="US_NEWS",
            title="Synthetic filing",
            body="Example Corp reported synthetic revenue of 100.",
            published_at=known_at,
            known_at=known_at,
            source_locator="fixture:1",
            content_hash="a" * 64,
        )
    )
    claim = Claim(
        claim_id="CLAIM_SQLITE",
        subject="Example Corp",
        predicate="reported_revenue",
        object_value="100",
        kind="confirmed_fact",
        event_time=known_at,
        known_at=known_at,
        evidence_ids=["DOC_SQLITE"],
        quote="reported synthetic revenue of 100",
    )
    repository.create_patch(
        WikiPatch(
            patch_id="PATCH_SQLITE",
            thread_id="THREAD_SQLITE",
            operation="create_event",
            event_id="EVENT_SQLITE",
            base_version=0,
            evidence_ids=["DOC_SQLITE"],
            payload={
                "event": {
                    "event_id": "EVENT_SQLITE",
                    "event_family": "earnings",
                    "event_subject": "Example Corp",
                    "event_title": "SQLite synthetic earnings",
                    "event_time": known_at.isoformat(),
                    "known_at": known_at.isoformat(),
                },
                "claims": [claim.model_dump(mode="json")],
                "relations": [],
            },
            audit=AuditResult(status="PASS"),
            model_version="test",
            prompt_version="test",
        )
    )
    repository.review_patch("PATCH_SQLITE", "approve")
    repository.commit_approved_patch("PATCH_SQLITE")

    output = tmp_path / "sqlite.jsonl"
    assert export_structured_jsonl(repository, output, CUTOFF) == 1
    record = json.loads(output.read_text())
    assert record["event_title"] == "SQLite synthetic earnings"
    assert record["event_subject"] == "Example Corp"
    assert record["claims"][0]["claim_id"] == "CLAIM_SQLITE"
    assert record["evidence"][0]["evidence_id"] == "DOC_SQLITE"
    assert record["review_history"][0]["decision"] == "approve"
    assert record["version"] == 1


def test_export_fails_closed_for_missing_or_invalid_known_at() -> None:
    cutoff = datetime(2025, 11, 4, tzinfo=UTC)
    base = {
        "status": "approved",
        "audit": {"status": "PASS"},
        "claims": [],
        "relations": [],
    }

    class RepositoryWithBadTimes:
        def list_export_events(self):
            return [
                {**base, "event_id": "MISSING"},
                {**base, "event_id": "INVALID", "known_at": "not-a-timestamp"},
            ]

    from event_wiki.export import iter_exportable_events

    assert list(iter_exportable_events(RepositoryWithBadTimes(), cutoff)) == []
