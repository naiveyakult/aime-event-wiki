from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from event_wiki.models import (
    AuditResult,
    AuditStatus,
    CandidateBundle,
    Claim,
    ClaimKind,
    EventDecision,
    EventFamily,
    EvidenceDocument,
    Relation,
    RelationType,
    WikiOperation,
    WikiPatch,
)

NOW = datetime(2025, 11, 3, 21, 0, tzinfo=UTC)


def evidence(evidence_id: str = "DOC_1") -> EvidenceDocument:
    return EvidenceDocument(
        evidence_id=evidence_id,
        content_type="US_NEWS",
        title="Example Corp reports third-quarter results",
        body="Example Corp reported revenue of $10 million.",
        published_at=NOW,
        known_at=NOW,
        source_name="Synthetic Wire",
        symbols=["EXM"],
        source_locator="synthetic://fixture/1",
        content_hash="a" * 64,
    )


def test_evidence_rejects_naive_timestamps() -> None:
    payload = evidence().model_dump()
    payload["published_at"] = datetime(2025, 1, 1)
    with pytest.raises(ValidationError):
        EvidenceDocument.model_validate(payload)


def test_candidate_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        CandidateBundle(
            candidate_id="C1",
            evidence_ids=[],
            window_start=NOW,
            window_end=NOW,
        )


def test_claim_and_relation_require_provenance() -> None:
    with pytest.raises(ValidationError):
        Claim(
            claim_id="CL1",
            subject="Example Corp",
            predicate="reported_revenue",
            object_value="$10 million",
            kind=ClaimKind.CONFIRMED_FACT,
            event_time=NOW,
            known_at=NOW,
            evidence_ids=[],
            quote="reported revenue of $10 million",
        )
    with pytest.raises(ValidationError):
        Relation(
            relation_id="R1",
            source_entity="Example Corp",
            target_entity="EXM",
            relation_type=RelationType.ISSUER,
            known_at=NOW,
            evidence_ids=[],
            rationale="EXM is the issuer symbol.",
        )


def test_wiki_patch_is_strict_and_versioned() -> None:
    patch = WikiPatch(
        patch_id="P1",
        thread_id="C1",
        operation=WikiOperation.CREATE_EVENT,
        base_version=0,
        evidence_ids=["DOC_1"],
        payload={"event_family": EventFamily.EARNINGS},
        audit=AuditResult(status=AuditStatus.PASS),
        model_version="fake-model",
        prompt_version="v1",
    )
    assert patch.base_version == 0


def test_event_decision_reject_requires_reason() -> None:
    with pytest.raises(ValidationError):
        EventDecision(decision="REJECT", reason="", candidate_id="C1")
