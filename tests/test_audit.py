from datetime import UTC, datetime, timedelta

from event_wiki.audit import audit_knowledge
from event_wiki.models import Claim, ClaimKind, EvidenceDocument

EVENT_TIME = datetime(2025, 11, 3, 21, 0, tzinfo=UTC)


def evidence() -> EvidenceDocument:
    return EvidenceDocument(
        evidence_id="DOC_1",
        content_type="US_NEWS",
        title="Example reports revenue",
        body="Example reported revenue of $10 million.",
        published_at=EVENT_TIME,
        known_at=EVENT_TIME,
        source_name="Synthetic Wire",
        source_locator="synthetic://1",
        content_hash="b" * 64,
    )


def claim(known_at: datetime, quote: str = "revenue of $10 million") -> Claim:
    return Claim(
        claim_id="CL1",
        subject="Example",
        predicate="reported_revenue",
        object_value="$10 million",
        kind=ClaimKind.CONFIRMED_FACT,
        event_time=EVENT_TIME,
        known_at=known_at,
        evidence_ids=["DOC_1"],
        quote=quote,
    )


def test_audit_blocks_future_claim() -> None:
    result = audit_knowledge(
        [claim(EVENT_TIME + timedelta(days=1))], [], {"DOC_1": evidence()}, EVENT_TIME
    )
    assert result.status == "BLOCK"
    assert any(issue.code == "future_information" for issue in result.issues)


def test_audit_blocks_quote_not_supported_by_evidence() -> None:
    result = audit_knowledge(
        [claim(EVENT_TIME, "profit was $99 million")], [], {"DOC_1": evidence()}, EVENT_TIME
    )
    assert result.status == "BLOCK"
    assert any(issue.code == "unsupported_quote" for issue in result.issues)


def test_audit_blocks_claim_that_predates_its_evidence() -> None:
    result = audit_knowledge(
        [claim(EVENT_TIME - timedelta(seconds=1))],
        [],
        {"DOC_1": evidence()},
        EVENT_TIME,
    )

    assert any(issue.code == "claim_predates_evidence" for issue in result.issues)


def test_audit_blocks_number_not_present_in_quote() -> None:
    unsupported = claim(EVENT_TIME).model_copy(update={"object_value": "$99 million"})
    result = audit_knowledge([unsupported], [], {"DOC_1": evidence()}, EVENT_TIME)

    assert any(issue.code == "unsupported_number" for issue in result.issues)


def test_audit_treats_scaled_and_expanded_numbers_as_equivalent() -> None:
    expanded = claim(EVENT_TIME).model_copy(update={"object_value": "$10,000,000"})

    result = audit_knowledge([expanded], [], {"DOC_1": evidence()}, EVENT_TIME)

    assert not any(issue.code == "unsupported_number" for issue in result.issues)


def test_audit_normalizes_decimal_financial_scales() -> None:
    document = evidence().model_copy(
        update={"body": "Example reported assets of $1.5 billion."}
    )
    scaled = claim(EVENT_TIME, "assets of $1.5 billion").model_copy(
        update={"predicate": "reported_assets", "object_value": "$1,500,000,000"}
    )

    result = audit_knowledge([scaled], [], {"DOC_1": document}, EVENT_TIME)

    assert not any(issue.code == "unsupported_number" for issue in result.issues)
