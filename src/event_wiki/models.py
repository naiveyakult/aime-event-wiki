from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)
    schema_version: str = SCHEMA_VERSION


class EventFamily(StrEnum):
    EARNINGS = "earnings"
    REGULATORY = "regulatory"
    PRODUCT_PARTNERSHIP = "product_partnership"


class DecisionType(StrEnum):
    CREATE = "CREATE"
    MERGE = "MERGE"
    SUPPLEMENT = "SUPPLEMENT"
    REJECT = "REJECT"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ClaimKind(StrEnum):
    CONFIRMED_FACT = "confirmed_fact"
    COMPANY_STATEMENT = "company_statement"
    REPORTED_CLAIM = "reported_claim"
    ANALYST_OPINION = "analyst_opinion"


class RelationType(StrEnum):
    ISSUER = "issuer"
    SUPPLIER = "supplier"
    CUSTOMER = "customer"
    COMPETITOR = "competitor"
    PARTNER = "partner"
    SECTOR_PROXY = "sector_proxy"
    POLICY_EXPOSURE = "policy_exposure"


class AuditStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"


class WikiOperation(StrEnum):
    CREATE_EVENT = "create_event"
    APPEND_CLAIM = "append_claim"
    SUPERSEDE_CLAIM = "supersede_claim"
    ADD_RELATION = "add_relation"
    MERGE_EVENT = "merge_event"
    UPDATE_METADATA = "update_metadata"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


class EvidenceDocument(StrictModel):
    evidence_id: str = Field(min_length=1)
    content_type: Literal["US_NEWS", "US_FLASH", "US_NOTICE", "V2_NOTICE"]
    title: str = Field(min_length=1)
    body: str = ""
    published_at: datetime
    known_at: datetime
    source_name: str = ""
    source_url: str | None = None
    symbols: list[str] = Field(default_factory=list)
    entity_names: list[str] = Field(default_factory=list)
    accession: str | None = None
    source_locator: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")

    _published_aware = field_validator("published_at")(_aware)
    _known_aware = field_validator("known_at")(_aware)

    @model_validator(mode="after")
    def known_not_before_publication(self) -> EvidenceDocument:
        if self.known_at < self.published_at:
            raise ValueError("known_at cannot be earlier than published_at")
        return self


class CandidateBundle(StrictModel):
    candidate_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    window_start: datetime
    window_end: datetime
    symbols: list[str] = Field(default_factory=list)
    entity_names: list[str] = Field(default_factory=list)
    retrieval_reason: str = ""

    _start_aware = field_validator("window_start")(_aware)
    _end_aware = field_validator("window_end")(_aware)


class EventProposal(StrictModel):
    proposal_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    event_family: EventFamily
    event_subject: str = Field(min_length=1)
    event_title: str = Field(min_length=1)
    event_time: datetime
    known_at: datetime
    primary_symbols: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)

    _event_aware = field_validator("event_time")(_aware)
    _known_aware = field_validator("known_at")(_aware)


class EventDecision(StrictModel):
    candidate_id: str = Field(min_length=1)
    decision: DecisionType
    reason: str
    existing_event_id: str | None = None

    @model_validator(mode="after")
    def decision_is_explained(self) -> EventDecision:
        if not self.reason.strip():
            raise ValueError("event decisions require a reason")
        if (
            self.decision in {DecisionType.MERGE, DecisionType.SUPPLEMENT}
            and not self.existing_event_id
        ):
            raise ValueError("merge/supplement decisions require existing_event_id")
        return self


class Claim(StrictModel):
    claim_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object_value: str = Field(min_length=1)
    unit: str | None = None
    kind: ClaimKind
    event_time: datetime
    known_at: datetime
    evidence_ids: list[str] = Field(min_length=1)
    quote: str = Field(min_length=1)
    confidence: float = Field(default=0.8, ge=0, le=1)

    _event_aware = field_validator("event_time")(_aware)
    _known_aware = field_validator("known_at")(_aware)


class Relation(StrictModel):
    relation_id: str = Field(min_length=1)
    source_entity: str = Field(min_length=1)
    target_entity: str = Field(min_length=1)
    relation_type: RelationType
    valid_from: datetime | None = None
    known_at: datetime
    evidence_ids: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    inferred_for_event: bool = False
    confidence: float = Field(default=0.8, ge=0, le=1)

    _valid_aware = field_validator("valid_from")(
        lambda value: _aware(value) if value is not None else value
    )
    _known_aware = field_validator("known_at")(_aware)


class AuditIssue(StrictModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: Literal["warning", "block"]
    field_ref: str | None = None
    evidence_id: str | None = None


class AuditResult(StrictModel):
    status: AuditStatus
    issues: list[AuditIssue] = Field(default_factory=list)


class EventMetadata(StrictModel):
    event_id: str | None = None
    event_family: EventFamily
    event_subject: str = Field(min_length=1)
    event_title: str = Field(min_length=1)
    event_time: datetime
    known_at: datetime
    primary_symbols: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["active", "archived"] | None = None

    _event_aware = field_validator("event_time")(_aware)
    _known_aware = field_validator("known_at")(_aware)


class KnowledgeEdge(StrictModel):
    edge_id: str = Field(min_length=1)
    source_node_kind: Literal["event", "entity", "claim", "evidence"]
    source_node_id: str = Field(min_length=1)
    target_node_kind: Literal["event", "entity", "claim", "evidence"]
    target_node_id: str = Field(min_length=1)
    relation_type: str = Field(min_length=1)
    known_at: datetime
    valid_from: datetime | None = None
    evidence_ids: list[str] = Field(min_length=1)

    _known_aware = field_validator("known_at")(_aware)
    _valid_aware = field_validator("valid_from")(
        lambda value: _aware(value) if value is not None else value
    )


class PatchPayload(StrictModel):
    event: EventMetadata
    proposals: list[EventProposal] = Field(default_factory=list)
    decisions: list[EventDecision] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    edges: list[KnowledgeEdge] = Field(default_factory=list)


class WikiPatch(StrictModel):
    patch_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    operation: WikiOperation
    event_id: str | None = None
    base_version: int = Field(ge=0)
    evidence_ids: list[str] = Field(min_length=1)
    payload: dict[str, Any]
    audit: AuditResult
    model_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EventGraphState(StrictModel):
    thread_id: str
    candidate_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    proposals: list[EventProposal] = Field(default_factory=list)
    decisions: list[EventDecision] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    audit: AuditResult | None = None
    patch_id: str | None = None
    review_decision: Literal["approve", "reject"] | None = None
