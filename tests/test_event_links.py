from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from event_wiki.agents import AgentSuite, HeuristicStructuredClient
from event_wiki.db import Base, Repository
from event_wiki.event_links import audit_event_link
from event_wiki.link_graph import EventLinkRunner
from event_wiki.models import (
    AuditResult,
    EventLink,
    EventLinkType,
    EvidenceDocument,
    EvidenceQuote,
    WikiPatch,
)

NOW = datetime(2025, 11, 3, 21, 0, tzinfo=UTC)


def event_link(**overrides) -> EventLink:  # noqa: ANN003
    values = {
        "link_id": "LINK_1",
        "source_event_id": "EVENT_A",
        "target_event_id": "EVENT_B",
        "link_type": EventLinkType.EVIDENCE_UPDATE,
        "inferred": False,
        "confidence": 0.95,
        "rationale": "The source explicitly updates the earlier event.",
        "evidence_quotes": [
            EvidenceQuote(
                evidence_id="DOC_SHARED",
                quote="Example Corp updated Sample Cloud partnership",
            )
        ],
        "known_at": NOW,
    }
    values.update(overrides)
    return EventLink(**values)


def test_event_link_rejects_self_reference() -> None:
    with pytest.raises(ValidationError, match="same event"):
        event_link(target_event_id="EVENT_A")


def test_event_link_requires_evidence_quotes() -> None:
    with pytest.raises(ValidationError):
        event_link(evidence_quotes=[])


def document(body: str = "Example Corp updated Sample Cloud partnership") -> EvidenceDocument:
    return EvidenceDocument(
        evidence_id="DOC_SHARED",
        content_type="US_NEWS",
        title="Example Corp and Sample Cloud update",
        body=body,
        published_at=NOW,
        known_at=NOW,
        source_locator="synthetic://shared",
        content_hash="a" * 64,
    )


def test_strict_shared_evidence_fact_is_auto_committed() -> None:
    source = {
        "event_id": "EVENT_A",
        "event_subject": "Example Corp",
        "evidence_ids": ["DOC_SHARED"],
    }
    target = {
        "event_id": "EVENT_B",
        "event_subject": "Sample Cloud",
        "evidence_ids": ["DOC_SHARED"],
    }

    result = audit_event_link(event_link(), source, target, {"DOC_SHARED": document()})

    assert result.status == "PASS"
    assert result.disposition == "auto_commit"


def test_inferred_low_risk_link_uses_batch_review() -> None:
    link = event_link(
        inferred=True,
        link_type=EventLinkType.SAME_DRIVER,
        confidence=0.9,
    )
    source = {"event_id": "EVENT_A", "event_subject": "Example Corp", "evidence_ids": []}
    target = {"event_id": "EVENT_B", "event_subject": "Sample Cloud", "evidence_ids": []}

    result = audit_event_link(link, source, target, {"DOC_SHARED": document()})

    assert result.disposition == "batch_review"


def test_inferred_causal_link_requires_individual_review() -> None:
    link = event_link(
        inferred=True,
        link_type=EventLinkType.CAUSES,
        confidence=0.95,
    )
    source = {"event_id": "EVENT_A", "event_subject": "Example Corp", "evidence_ids": []}
    target = {"event_id": "EVENT_B", "event_subject": "Sample Cloud", "evidence_ids": []}

    result = audit_event_link(link, source, target, {"DOC_SHARED": document()})

    assert result.disposition == "individual_review"


def test_invalid_quote_is_blocked_instead_of_queued() -> None:
    source = {"event_id": "EVENT_A", "event_subject": "Example Corp", "evidence_ids": []}
    target = {"event_id": "EVENT_B", "event_subject": "Sample Cloud", "evidence_ids": []}

    result = audit_event_link(
        event_link(), source, target, {"DOC_SHARED": document("Different source text")}
    )

    assert result.status == "BLOCK"
    assert result.disposition == "blocked"


def test_quote_must_be_an_exact_substring() -> None:
    source = {"event_id": "EVENT_A", "event_subject": "Example Corp", "evidence_ids": []}
    target = {"event_id": "EVENT_B", "event_subject": "Sample Cloud", "evidence_ids": []}

    result = audit_event_link(
        event_link(
            evidence_quotes=[
                EvidenceQuote(
                    evidence_id="DOC_SHARED",
                    quote="example corp updated sample cloud partnership",
                )
            ]
        ),
        source,
        target,
        {"DOC_SHARED": document()},
    )

    assert result.status == "BLOCK"
    assert result.issues[0].code == "unsupported_link_quote"


def test_future_evidence_and_missing_endpoint_are_blocked() -> None:
    future_document = document().model_copy(
        update={"known_at": datetime(2025, 11, 4, 21, 0, tzinfo=UTC)}
    )
    result = audit_event_link(
        event_link(),
        {"event_id": "EVENT_A", "event_subject": "Example Corp", "evidence_ids": []},
        None,
        {"DOC_SHARED": future_document},
    )

    assert result.status == "BLOCK"
    assert {item.code for item in result.issues} == {
        "missing_event_endpoint",
        "future_link_evidence",
    }


def test_low_confidence_valid_link_is_suppressed() -> None:
    result = audit_event_link(
        event_link(confidence=0.64),
        {"event_id": "EVENT_A", "event_subject": "Example Corp", "evidence_ids": []},
        {"event_id": "EVENT_B", "event_subject": "Sample Cloud", "evidence_ids": []},
        {"DOC_SHARED": document()},
    )

    assert result.disposition == "suppressed"


def _event_patch(event_id: str, subject: str, patch_id: str) -> WikiPatch:
    return WikiPatch(
        patch_id=patch_id,
        thread_id=patch_id,
        operation="create_event",
        event_id=event_id,
        base_version=0,
        evidence_ids=["DOC_SHARED"],
        payload={
            "event": {
                "event_id": event_id,
                "event_family": "product_partnership",
                "event_subject": subject,
                "event_title": f"{subject} partnership update",
                "event_time": NOW.isoformat(),
                "known_at": NOW.isoformat(),
                "evidence_ids": ["DOC_SHARED"],
            },
            "claims": [],
            "relations": [],
            "edges": [],
        },
        audit=AuditResult(status="PASS"),
        model_version="test",
        prompt_version="test",
    )


def test_event_link_uses_an_independent_version_from_events() -> None:
    repo = Repository.from_url("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(repo.engine)
    try:
        repo.upsert_evidence(document())
        for patch in (
            _event_patch("EVENT_A", "Example Corp", "PATCH_A"),
            _event_patch("EVENT_B", "Sample Cloud", "PATCH_B"),
        ):
            repo.create_patch(patch)
            repo.review_patch(patch.patch_id, "approve")
            repo.commit_approved_patch(patch.patch_id)
        link = event_link()
        link_patch = WikiPatch(
            patch_id="PATCH_LINK",
            thread_id="link:EVENT_A:v1",
            operation="add_event_link",
            event_id="EVENT_A",
            link_id=link.link_id,
            base_version=0,
            evidence_ids=["DOC_SHARED"],
            payload={"event_link": link.model_dump(mode="json")},
            audit=AuditResult(status="PASS"),
            model_version="test",
            prompt_version="test",
        )
        repo.create_patch(link_patch)
        repo.review_patch("PATCH_LINK", "approve")

        assert repo.commit_approved_patch("PATCH_LINK") == 1
        assert repo.get_event_version("EVENT_A") == 1
        assert repo.get_event_version("EVENT_B") == 1
        assert repo.get_event_link("LINK_1")["current_version"] == 1
        graph = repo.graph_snapshot(NOW)
        event_link_edge = next(edge for edge in graph["edges"] if edge["edge_id"] == "LINK_1")
        assert event_link_edge["source_node_id"] == "EVENT_A"
        assert event_link_edge["target_node_id"] == "EVENT_B"
    finally:
        repo.close()


def test_event_link_runner_auto_commits_strict_fact() -> None:
    class SharedEvidenceClient(HeuristicStructuredClient):
        def _link_events(self, context):  # noqa: ANN001
            source = context["source_event"]
            target = context["candidate_events"][0]
            return {
                "links": [
                    event_link(
                        source_event_id=source["event_id"],
                        target_event_id=target["event_id"],
                    ).model_dump(mode="json")
                ]
            }

    repo = Repository.from_url("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(repo.engine)
    try:
        repo.upsert_evidence(document())
        for patch in (
            _event_patch("EVENT_A", "Example Corp", "PATCH_A"),
            _event_patch("EVENT_B", "Sample Cloud", "PATCH_B"),
        ):
            repo.create_patch(patch)
            repo.review_patch(patch.patch_id, "approve")
            repo.commit_approved_patch(patch.patch_id)

        result = EventLinkRunner(repo, AgentSuite(SharedEvidenceClient())).run("EVENT_A")

        assert len(result["auto_committed"]) == 1
        assert result["patch_ids"] == []
        stored = repo.get_patch(result["auto_committed"][0])
        assert stored["status"] == "committed"
        assert stored["audit"]["disposition"] == "auto_commit"
    finally:
        repo.close()
