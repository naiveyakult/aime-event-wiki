import os
from datetime import UTC, datetime, timedelta

import pytest

from event_wiki.db import Base, Repository, VersionConflict, WikiPatchRow
from event_wiki.models import (
    AuditResult,
    AuditStatus,
    CandidateBundle,
    EventFamily,
    EvidenceDocument,
    WikiOperation,
    WikiPatch,
)

NOW = datetime(2025, 11, 3, 21, 0, tzinfo=UTC)


def evidence(evidence_id: str = "DOC_1", *, known_at: datetime = NOW) -> EvidenceDocument:
    return EvidenceDocument(
        evidence_id=evidence_id,
        content_type="US_NEWS",
        title="Example Corp announces a partnership",
        body="Example Corp partnered with Sample Cloud.",
        published_at=NOW,
        known_at=known_at,
        source_name="Synthetic Wire",
        symbols=["EXM"],
        entity_names=["Example Corp", "Sample Cloud"],
        source_locator=f"synthetic://{evidence_id}",
        content_hash=("a" if evidence_id == "DOC_1" else "b") * 64,
    )


@pytest.fixture
def repository() -> Repository:
    repo = Repository.from_url("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(repo.engine)
    try:
        yield repo
    finally:
        repo.close()


def patch(patch_id: str = "PATCH_1", *, base_version: int = 0) -> WikiPatch:
    return WikiPatch(
        patch_id=patch_id,
        thread_id="CAND_1",
        operation=WikiOperation.CREATE_EVENT
        if base_version == 0
        else WikiOperation.UPDATE_METADATA,
        event_id="EVT_1",
        base_version=base_version,
        evidence_ids=["DOC_1"],
        payload={
            "event": {
                "event_family": EventFamily.PRODUCT_PARTNERSHIP,
                "event_subject": "Example Corp",
                "event_title": "Example Corp partners with Sample Cloud",
                "event_time": NOW.isoformat(),
                "known_at": NOW.isoformat(),
            },
            "edges": [
                {
                    "edge_id": f"EDGE_{patch_id}",
                    "source_node_kind": "event",
                    "source_node_id": "EVT_1",
                    "target_node_kind": "entity",
                    "target_node_id": "Sample Cloud",
                    "relation_type": "partner",
                    "known_at": NOW.isoformat(),
                    "valid_from": NOW.isoformat(),
                    "evidence_ids": ["DOC_1"],
                }
            ],
        },
        audit=AuditResult(status=AuditStatus.PASS),
        model_version="fake-model",
        prompt_version="v1",
    )


def test_evidence_upsert_and_time_filtered_listing(repository: Repository) -> None:
    repository.upsert_evidence(evidence())
    changed = evidence().model_copy(update={"body": "Updated synthetic body."})
    repository.upsert_evidence(changed)

    rows = repository.list_evidence(
        start=NOW - timedelta(minutes=1), end=NOW + timedelta(minutes=1)
    )

    assert len(rows) == 1
    assert rows[0].body == "Updated synthetic body."
    assert repository.status_counts()["evidence_documents"] == 1


def test_evidence_keyset_pagination(repository: Repository) -> None:
    for evidence_id in ("DOC_1", "DOC_2"):
        repository.upsert_evidence(evidence(evidence_id))
    pages = list(repository.iter_evidence_pages(page_size=1))
    assert [[item.evidence_id for item in page] for page in pages] == [
        ["DOC_1"],
        ["DOC_2"],
    ]


def test_candidate_crud_and_event_lookup(repository: Repository) -> None:
    repository.upsert_evidence(evidence())
    candidate = CandidateBundle(
        candidate_id="CAND_1",
        evidence_ids=["DOC_1"],
        window_start=NOW,
        window_end=NOW,
        symbols=["EXM"],
        entity_names=["Example Corp"],
        retrieval_reason="synthetic fixture",
    )
    repository.save_candidate(candidate)
    assert repository.get_candidate("CAND_1") == candidate
    assert repository.list_candidates()[0].candidate_id == "CAND_1"
    assert repository.delete_candidate("CAND_1") is True
    assert repository.get_candidate("CAND_1") is None


def test_patch_review_commit_and_optimistic_version(repository: Repository) -> None:
    repository.upsert_evidence(evidence())
    repository.create_patch(patch())
    repository.review_patch("PATCH_1", decision="approve", reviewer="reviewer@example.test")

    version = repository.commit_approved_patch("PATCH_1")

    assert version == 1
    assert repository.get_patch("PATCH_1")["status"] == "committed"
    matches = repository.find_existing_events(
        subject="Example Corp", event_family=EventFamily.PRODUCT_PARTNERSHIP, around=NOW
    )
    assert [row["event_id"] for row in matches] == ["EVT_1"]

    repository.create_patch(patch("PATCH_STALE", base_version=1))
    repository.review_patch("PATCH_STALE", decision="approve", reviewer="reviewer@example.test")
    repository.create_patch(patch("PATCH_WINNER", base_version=1))
    repository.review_patch("PATCH_WINNER", decision="approve", reviewer="reviewer@example.test")
    assert repository.commit_approved_patch("PATCH_WINNER") == 2
    with pytest.raises(VersionConflict):
        repository.commit_approved_patch("PATCH_STALE")

    versions = repository.list_approved_versions()
    historical = repository.list_approved_versions(cutoff=NOW - timedelta(seconds=1))
    assert [(row["event_id"], row["version"]) for row in versions] == [("EVT_1", 2)]
    assert historical == []


def test_graph_snapshot_respects_known_at(repository: Repository) -> None:
    repository.upsert_evidence(evidence())
    repository.upsert_evidence(evidence("DOC_2", known_at=NOW + timedelta(days=3)))
    repository.create_patch(patch())
    repository.review_patch("PATCH_1", decision="approve", reviewer="reviewer@example.test")
    repository.commit_approved_patch("PATCH_1")

    early = repository.graph_snapshot(NOW - timedelta(seconds=1))
    current = repository.graph_snapshot(NOW)

    assert early == {"nodes": [], "edges": []}
    assert {node["node_id"] for node in current["nodes"]} == {"EVT_1", "Sample Cloud"}
    assert current["edges"][0]["relation_type"] == "partner"
    assert current["edges"][0]["evidence_ids"] == ["DOC_1"]


def test_graph_snapshot_uses_version_visible_at_cutoff(repository: Repository) -> None:
    repository.upsert_evidence(evidence())
    repository.create_patch(patch())
    repository.review_patch("PATCH_1", "approve")
    repository.commit_approved_patch("PATCH_1")
    later = patch("PATCH_LATER", base_version=1).model_copy(
        update={
            "payload": {
                **patch("PATCH_LATER", base_version=1).payload,
                "event": {
                    **patch("PATCH_LATER", base_version=1).payload["event"],
                    "event_title": "Later corrected title",
                    "known_at": (NOW + timedelta(days=2)).isoformat(),
                },
                "edges": [],
            }
        }
    )
    repository.create_patch(later)
    repository.review_patch("PATCH_LATER", "approve")
    repository.commit_approved_patch("PATCH_LATER")

    historical = repository.graph_snapshot(NOW + timedelta(days=1))
    current = repository.graph_snapshot(NOW + timedelta(days=3))

    historical_event = next(node for node in historical["nodes"] if node["node_kind"] == "event")
    current_event = next(node for node in current["nodes"] if node["node_kind"] == "event")
    assert historical_event["label"] == "Example Corp partners with Sample Cloud"
    assert current_event["label"] == "Later corrected title"


def test_blocked_patch_cannot_be_approved(repository: Repository) -> None:
    repository.upsert_evidence(evidence())
    blocked = patch().model_copy(update={"audit": AuditResult(status=AuditStatus.BLOCK)})
    repository.create_patch(blocked)
    with pytest.raises(ValueError, match="BLOCK"):
        repository.review_patch("PATCH_1", decision="approve", reviewer="reviewer@example.test")


def edited_payload(*, claim_overrides=None, edge_overrides=None) -> dict:
    claim = {
        "claim_id": "CLAIM_EDITED",
        "subject": "Example Corp",
        "predicate": "partnered_with",
        "object_value": "Sample Cloud",
        "kind": "confirmed_fact",
        "event_time": NOW.isoformat(),
        "known_at": NOW.isoformat(),
        "evidence_ids": ["DOC_1"],
        "quote": "partnered with Sample Cloud",
        "confidence": 0.9,
    }
    claim.update(claim_overrides or {})
    edge = {
        "edge_id": "EDGE_EDITED",
        "source_node_kind": "event",
        "source_node_id": "EVT_1",
        "target_node_kind": "entity",
        "target_node_id": "Sample Cloud",
        "relation_type": "partner",
        "known_at": NOW.isoformat(),
        "valid_from": NOW.isoformat(),
        "evidence_ids": ["DOC_1"],
    }
    edge.update(edge_overrides or {})
    return {**patch().payload, "claims": [claim], "relations": [], "edges": [edge]}


@pytest.mark.parametrize(
    "payload",
    [
        edited_payload(claim_overrides={"known_at": (NOW + timedelta(days=1)).isoformat()}),
        edited_payload(claim_overrides={"quote": "text absent from the evidence"}),
        edited_payload(claim_overrides={"evidence_ids": ["DOC_UNKNOWN"]}),
        edited_payload(edge_overrides={"known_at": (NOW + timedelta(days=1)).isoformat()}),
        edited_payload(edge_overrides={"source_node_kind": "unsupported_kind"}),
    ],
)
def test_edited_payload_is_reaudited_before_approval(repository: Repository, payload: dict) -> None:
    repository.upsert_evidence(evidence())
    repository.create_patch(patch())

    with pytest.raises(ValueError, match="validation failed"):
        repository.review_patch("PATCH_1", "approve", edited_payload=payload)

    stored = repository.get_patch("PATCH_1")
    assert stored["status"] == "pending"
    assert stored["audit"]["status"] == "BLOCK"


def test_edited_payload_cannot_reference_existing_evidence_outside_patch(
    repository: Repository,
) -> None:
    repository.upsert_evidence(evidence())
    repository.upsert_evidence(evidence("DOC_2"))
    repository.create_patch(patch())
    payload = edited_payload(claim_overrides={"evidence_ids": ["DOC_2"]})

    with pytest.raises(ValueError, match="validation failed"):
        repository.review_patch("PATCH_1", "approve", edited_payload=payload)


def test_event_version_and_unsupported_operations_fail_closed(repository: Repository) -> None:
    repository.upsert_evidence(evidence())
    repository.create_patch(patch())
    repository.review_patch("PATCH_1", "approve")
    repository.commit_approved_patch("PATCH_1")
    assert repository.get_event_version("EVT_1") == 1

    unsupported = patch("PATCH_MERGE", base_version=1).model_copy(
        update={"operation": WikiOperation.MERGE_EVENT}
    )
    repository.create_patch(unsupported)
    with pytest.raises(ValueError, match="not supported"):
        repository.review_patch("PATCH_MERGE", "approve")
    assert repository.get_patch("PATCH_MERGE")["status"] == "pending"


def test_update_metadata_uses_current_version(repository: Repository) -> None:
    repository.upsert_evidence(evidence())
    repository.create_patch(patch())
    repository.review_patch("PATCH_1", "approve")
    repository.commit_approved_patch("PATCH_1")
    update = patch("PATCH_UPDATE", base_version=repository.get_event_version("EVT_1"))
    repository.create_patch(update)
    repository.review_patch("PATCH_UPDATE", "approve")
    assert repository.commit_approved_patch("PATCH_UPDATE") == 2


def test_commit_revalidates_payload_inside_transaction(repository: Repository) -> None:
    repository.upsert_evidence(evidence())
    repository.create_patch(patch())
    repository.review_patch("PATCH_1", "approve")
    with repository.session() as session:
        row = session.get(WikiPatchRow, "PATCH_1")
        row.payload = edited_payload(
            claim_overrides={"known_at": (NOW + timedelta(days=1)).isoformat()}
        )
    with pytest.raises(ValueError, match="validation failed"):
        repository.commit_approved_patch("PATCH_1")
    assert repository.status_counts()["events"] == 0


def test_review_context_and_rerun_preserve_history(repository: Repository) -> None:
    repository.upsert_evidence(evidence())
    repository.create_patch(patch())
    repository.reset_patch_for_rerun("PATCH_1", reviewer="bob")
    context = repository.get_review_context("PATCH_1")
    assert context["patch"]["status"] == "rerun_requested"
    assert context["evidence"][0]["body"]
    assert context["current_event"] is None
    assert [item["decision"] for item in context["review_history"]] == ["rerun"]
    assert repository.list_pending_patches() == []
    with pytest.raises(ValueError, match="only a pending patch"):
        repository.reset_patch_for_rerun("PATCH_1")


@pytest.mark.postgres
def test_postgres_repository_smoke() -> None:
    database_url = os.environ.get("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("set TEST_POSTGRES_URL to run PostgreSQL integration tests")
    with Repository.from_url(database_url, create_schema=True) as repository:
        document = evidence("POSTGRES_SYNTHETIC_DOC")
        repository.upsert_evidence(document)
        assert repository.list_evidence(evidence_ids=[document.evidence_id]) == [document]
