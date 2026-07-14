from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from event_wiki.audit import audit_knowledge
from event_wiki.llm import StructuredLLM
from event_wiki.models import (
    AuditResult,
    CandidateBundle,
    Claim,
    ClaimKind,
    DecisionType,
    EventDecision,
    EventFamily,
    EventProposal,
    EvidenceDocument,
    Relation,
    RelationType,
    WikiOperation,
    WikiPatch,
)

PROMPT_VERSION = "v1"
PROMPT_ROOT = Path(__file__).resolve().parents[2] / "prompts"
CLAIM_CHUNK_CHARS = 1_000
CLAIM_CHUNK_OVERLAP = 100
CLAIM_RETRY_MIN_CHARS = 500
MAX_CLAIMS_PER_CHUNK = 3


class EventProposalBatch(BaseModel):
    proposals: list[EventProposal] = Field(default_factory=list)


class EventDecisionBatch(BaseModel):
    decisions: list[EventDecision] = Field(default_factory=list)


class ClaimBatch(BaseModel):
    claims: list[Claim] = Field(default_factory=list, max_length=MAX_CLAIMS_PER_CHUNK)


class RelationBatch(BaseModel):
    relations: list[Relation] = Field(default_factory=list, max_length=MAX_CLAIMS_PER_CHUNK)


def _stable_id(prefix: str, *parts: str) -> str:
    value = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(value).hexdigest()[:20]}"


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump(item) for item in value]
    return value


def _normalized_text(value: str) -> str:
    return " ".join(value.lower().split())


def _text_chunks(value: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    text = value.strip()
    if not text or len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(start + max_chars, len(text))
        end = hard_end
        if hard_end < len(text):
            search_start = start + max_chars // 2
            boundaries = [
                text.rfind(marker, search_start, hard_end)
                for marker in ("\n\n", ". ", "! ", "? ")
            ]
            boundary = max(boundaries)
            if boundary >= search_start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap_chars)
    return chunks


def _chunk_document(
    document: EvidenceDocument,
    *,
    max_chars: int = CLAIM_CHUNK_CHARS,
    overlap_chars: int = CLAIM_CHUNK_OVERLAP,
) -> list[EvidenceDocument]:
    return [
        document.model_copy(update={"body": chunk})
        for chunk in _text_chunks(
            document.body,
            max_chars=max_chars,
            overlap_chars=min(overlap_chars, max_chars // 4),
        )
    ]


def _is_retryable_truncation(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        message = str(current).lower()
        if (
            "incomplete chunked read" in message
            or "complete message body" in message
            or "eof while parsing" in message
            or "empty structured response" in message
        ):
            return True
        current = current.__cause__
    return False


def _prompt(name: str) -> str:
    return (PROMPT_ROOT / f"{name}.md").read_text(encoding="utf-8")


class HeuristicStructuredClient:
    """Offline deterministic baseline and convenient test fake.

    It is intentionally conservative: it only emits facts directly backed by a
    document and never estimates market direction or fills absent fields.
    """

    model_version = "heuristic-v1"

    def invoke(self, *, task, prompt, output_model, context):  # noqa: ANN001
        del prompt
        handler = getattr(self, f"_{task}")
        return output_model.model_validate(handler(context))

    def _discover_event(self, context: dict[str, Any]) -> dict[str, Any]:
        candidate = context["candidate"]
        proposals = []
        patterns = {
            EventFamily.EARNINGS: ("earnings", "revenue", "quarterly results"),
            EventFamily.REGULATORY: ("regulator", "regulatory", "sec ", "investigation"),
            EventFamily.PRODUCT_PARTNERSHIP: (
                "launch",
                "product",
                "partnership",
                "partnered",
                "collaboration",
            ),
        }
        for document in context["evidence"]:
            text = f"{document['title']} {document.get('body', '')}".lower()
            family = next(
                (
                    family
                    for family, words in patterns.items()
                    if any(word in text for word in words)
                ),
                None,
            )
            if family is None:
                continue
            subject = (
                document.get("entity_names") or candidate.get("entity_names") or ["Unknown"]
            )[0]
            proposals.append(
                {
                    "proposal_id": _stable_id(
                        "proposal", candidate["candidate_id"], document["evidence_id"], family
                    ),
                    "candidate_id": candidate["candidate_id"],
                    "event_family": family,
                    "event_subject": subject,
                    "event_title": document["title"],
                    "event_time": document["published_at"],
                    "known_at": document["known_at"],
                    "primary_symbols": document.get("symbols", []),
                    "evidence_ids": [document["evidence_id"]],
                    "reason": "Deterministic keyword match backed by this evidence document.",
                }
            )
        return {"proposals": proposals}

    def _resolve_identity(self, context: dict[str, Any]) -> dict[str, Any]:
        existing = context.get("existing_events", [])
        decisions = []
        for proposal in context["proposals"]:
            match = next(
                (
                    item
                    for item in existing
                    if (item.get("event_subject") or item.get("subject"))
                    == proposal["event_subject"]
                    and (item.get("event_family") or item.get("family")) == proposal["event_family"]
                ),
                None,
            )
            decisions.append(
                {
                    "candidate_id": proposal["candidate_id"],
                    "decision": DecisionType.SUPPLEMENT if match else DecisionType.CREATE,
                    "existing_event_id": (match.get("event_id") or match.get("id"))
                    if match
                    else None,
                    "reason": "Matching existing event found."
                    if match
                    else "No matching event found.",
                }
            )
        return {"decisions": decisions}

    def _extract_claims(self, context: dict[str, Any]) -> dict[str, Any]:
        proposal = context["proposal"]
        claims = []
        for document in context["evidence"]:
            body = document.get("body", "").strip()
            quote = next(
                (part.strip() for part in re.split(r"(?<=[.!?])\s+", body) if part.strip()), ""
            )
            if not quote:
                continue
            claims.append(
                {
                    "claim_id": _stable_id(
                        "claim", proposal["proposal_id"], document["evidence_id"], quote
                    ),
                    "subject": proposal["event_subject"],
                    "predicate": "reported_event_fact",
                    "object_value": quote,
                    "kind": ClaimKind.REPORTED_CLAIM,
                    "event_time": proposal["event_time"],
                    "known_at": document["known_at"],
                    "evidence_ids": [document["evidence_id"]],
                    "quote": quote,
                    "confidence": 0.7,
                }
            )
        return {"claims": claims}

    def _build_relations(self, context: dict[str, Any]) -> dict[str, Any]:
        proposal = context["proposal"]
        relations = []
        for document in context["evidence"]:
            for symbol in proposal.get("primary_symbols", []):
                relations.append(
                    {
                        "relation_id": _stable_id(
                            "relation", proposal["event_subject"], symbol, "issuer"
                        ),
                        "source_entity": proposal["event_subject"],
                        "target_entity": symbol,
                        "relation_type": RelationType.ISSUER,
                        "valid_from": proposal["event_time"],
                        "known_at": document["known_at"],
                        "evidence_ids": [document["evidence_id"]],
                        "rationale": (
                            f"The source associates {proposal['event_subject']} with {symbol}."
                        ),
                        "inferred_for_event": False,
                        "confidence": 0.8,
                    }
                )
        return {"relations": relations}


class AgentSuite:
    def __init__(self, client: StructuredLLM, *, prompt_version: str = PROMPT_VERSION) -> None:
        self.client = client
        self.model_version = client.model_version
        self.prompt_version = prompt_version

    def discover(
        self, candidate: CandidateBundle, evidence: list[EvidenceDocument]
    ) -> list[EventProposal]:
        result = self.client.invoke(
            task="discover_event",
            prompt=_prompt("discovery"),
            output_model=EventProposalBatch,
            context={"candidate": _dump(candidate), "evidence": _dump(evidence)},
        )
        allowed = set(candidate.evidence_ids)
        return [proposal for proposal in result.proposals if set(proposal.evidence_ids) <= allowed]

    def resolve(self, proposals: list[EventProposal], repository: Any) -> list[EventDecision]:
        existing: list[Any] = []
        for proposal in proposals:
            finder = getattr(repository, "find_events", None)
            if finder:
                found = finder(
                    subject=proposal.event_subject,
                    event_family=proposal.event_family,
                    event_time=proposal.event_time,
                )
                existing.extend(found or [])
                continue
            finder = getattr(repository, "find_existing_events", None)
            if finder:
                found = finder(
                    subject=proposal.event_subject,
                    event_family=proposal.event_family,
                    around=proposal.event_time,
                )
                existing.extend(found or [])
        result = self.client.invoke(
            task="resolve_identity",
            prompt=_prompt("resolver"),
            output_model=EventDecisionBatch,
            context={"proposals": _dump(proposals), "existing_events": _dump(existing)},
        )
        return result.decisions

    def extract_claims(
        self, proposal: EventProposal, evidence: list[EvidenceDocument]
    ) -> list[Claim]:
        extracted: list[Claim] = []
        for document in evidence:
            for chunk in _chunk_document(document):
                extracted.extend(self._extract_claim_chunk(proposal, chunk))
        documents = {item.evidence_id: item for item in evidence}
        claims: dict[str, Claim] = {}
        for claim in extracted:
            if not set(claim.evidence_ids) <= set(documents):
                continue
            quote = _normalized_text(claim.quote)
            supporting_ids = [
                evidence_id
                for evidence_id in claim.evidence_ids
                if quote
                and quote
                in _normalized_text(
                    f"{documents[evidence_id].title} {documents[evidence_id].body}"
                )
            ]
            if not supporting_ids:
                continue
            claim_id = _stable_id(
                "claim",
                proposal.proposal_id,
                claim.subject,
                claim.predicate,
                claim.object_value,
                claim.quote,
                *sorted(supporting_ids),
            )
            supported = claim.model_copy(
                update={"claim_id": claim_id, "evidence_ids": supporting_ids}
            )
            previous = claims.get(claim_id)
            if previous is None or supported.confidence > previous.confidence:
                claims[claim_id] = supported
        return list(claims.values())

    def _extract_claim_chunk(
        self,
        proposal: EventProposal,
        document: EvidenceDocument,
        *,
        same_retry_used: bool = False,
    ) -> list[Claim]:
        try:
            result = self.client.invoke(
                task="extract_claims",
                prompt=_prompt("claims"),
                output_model=ClaimBatch,
                context={
                    "proposal": _dump(proposal),
                    "evidence": _dump([document]),
                    "max_claims": MAX_CLAIMS_PER_CHUNK,
                },
            )
            return result.claims
        except Exception as error:
            if not _is_retryable_truncation(error):
                raise
            if len(document.body) <= CLAIM_RETRY_MIN_CHARS:
                if same_retry_used:
                    raise
                return self._extract_claim_chunk(
                    proposal, document, same_retry_used=True
                )
            retry_chars = max(CLAIM_RETRY_MIN_CHARS, len(document.body) // 2)
            retry_chunks = _chunk_document(
                document,
                max_chars=retry_chars,
                overlap_chars=min(CLAIM_CHUNK_OVERLAP, retry_chars // 5),
            )
            if len(retry_chunks) <= 1:
                raise
            claims: list[Claim] = []
            for chunk in retry_chunks:
                claims.extend(self._extract_claim_chunk(proposal, chunk))
            return claims

    def build_relations(
        self, proposal: EventProposal, evidence: list[EvidenceDocument]
    ) -> list[Relation]:
        extracted: list[Relation] = []
        for document in evidence:
            for chunk in _chunk_document(document):
                extracted.extend(self._extract_relation_chunk(proposal, chunk))
        allowed = {item.evidence_id for item in evidence}
        relations = [
            relation for relation in extracted if set(relation.evidence_ids) <= allowed
        ]
        merged: dict[str, Relation] = {}
        for relation in relations:
            relation_id = _stable_id(
                "relation",
                relation.source_entity,
                relation.target_entity,
                str(relation.relation_type),
                str(relation.inferred_for_event),
            )
            relation = relation.model_copy(update={"relation_id": relation_id})
            previous = merged.get(relation_id)
            if previous is None:
                merged[relation_id] = relation
                continue
            valid_values = [value for value in (previous.valid_from, relation.valid_from) if value]
            merged[relation_id] = previous.model_copy(
                update={
                    "evidence_ids": sorted(set(previous.evidence_ids) | set(relation.evidence_ids)),
                    "known_at": min(previous.known_at, relation.known_at),
                    "valid_from": min(valid_values) if valid_values else None,
                    "confidence": max(previous.confidence, relation.confidence),
                }
            )
        return list(merged.values())

    def _extract_relation_chunk(
        self,
        proposal: EventProposal,
        document: EvidenceDocument,
        *,
        same_retry_used: bool = False,
    ) -> list[Relation]:
        try:
            result = self.client.invoke(
                task="build_relations",
                prompt=_prompt("relations"),
                output_model=RelationBatch,
                context={
                    "proposal": _dump(proposal),
                    "evidence": _dump([document]),
                    "max_relations": MAX_CLAIMS_PER_CHUNK,
                },
            )
            return result.relations
        except Exception as error:
            if not _is_retryable_truncation(error):
                raise
            if len(document.body) <= CLAIM_RETRY_MIN_CHARS:
                if same_retry_used:
                    raise
                return self._extract_relation_chunk(
                    proposal, document, same_retry_used=True
                )
            retry_chars = max(CLAIM_RETRY_MIN_CHARS, len(document.body) // 2)
            retry_chunks = _chunk_document(
                document,
                max_chars=retry_chars,
                overlap_chars=min(CLAIM_CHUNK_OVERLAP, retry_chars // 5),
            )
            if len(retry_chunks) <= 1:
                raise
            relations: list[Relation] = []
            for chunk in retry_chunks:
                relations.extend(self._extract_relation_chunk(proposal, chunk))
            return relations

    def audit(
        self,
        claims: list[Claim],
        relations: list[Relation],
        evidence: list[EvidenceDocument],
        prediction_cutoff: datetime,
    ) -> AuditResult:
        return audit_knowledge(
            claims,
            relations,
            {item.evidence_id: item for item in evidence},
            prediction_cutoff.astimezone(UTC),
        )

    def propose_patch(
        self,
        *,
        thread_id: str,
        proposals: list[EventProposal],
        decisions: list[EventDecision],
        claims: list[Claim],
        relations: list[Relation],
        audit: AuditResult,
        base_version: int = 0,
    ) -> WikiPatch:
        if len(proposals) != 1 or len(decisions) != 1:
            raise ValueError("propose_patch accepts exactly one proposal; use propose_patches")
        return self._make_patch(
            thread_id=thread_id,
            proposal=proposals[0],
            decision=decisions[0],
            claims=claims,
            relations=relations,
            audit=audit,
            base_version=base_version,
        )

    def propose_patches(
        self,
        *,
        thread_id: str,
        proposals: list[EventProposal],
        decisions: list[EventDecision],
        claims: list[Claim],
        relations: list[Relation],
        audit: AuditResult | None = None,
        audits: list[AuditResult] | None = None,
        base_versions: list[int] | None = None,
    ) -> list[WikiPatch]:
        if len(proposals) != len(decisions):
            raise ValueError("each proposal requires exactly one resolver decision")
        versions = base_versions or [0] * len(proposals)
        if len(versions) != len(proposals):
            raise ValueError("base_versions must align with proposals")
        proposal_audits = audits or ([audit] * len(proposals) if audit is not None else [])
        if len(proposal_audits) != len(proposals):
            raise ValueError("audits must align with proposals")
        patches = []
        for proposal, decision, base_version, proposal_audit in zip(
            proposals, decisions, versions, proposal_audits, strict=True
        ):
            allowed = set(proposal.evidence_ids)
            proposal_claims = [
                item
                for item in claims
                if item.subject == proposal.event_subject and set(item.evidence_ids) <= allowed
            ]
            proposal_relations = [
                item
                for item in relations
                if proposal.event_subject in {item.source_entity, item.target_entity}
                and set(item.evidence_ids) <= allowed
            ]
            patches.append(
                self._make_patch(
                    thread_id=thread_id,
                    proposal=proposal,
                    decision=decision,
                    claims=proposal_claims,
                    relations=proposal_relations,
                    audit=proposal_audit,
                    base_version=base_version,
                )
            )
        return patches

    def _make_patch(
        self,
        *,
        thread_id: str,
        proposal: EventProposal,
        decision: EventDecision,
        claims: list[Claim],
        relations: list[Relation],
        audit: AuditResult,
        base_version: int,
    ) -> WikiPatch:
        evidence_ids = sorted(proposal.evidence_ids)
        operation = WikiOperation.CREATE_EVENT
        if decision.decision == DecisionType.MERGE:
            operation = WikiOperation.MERGE_EVENT
        elif decision.decision == DecisionType.SUPPLEMENT:
            operation = WikiOperation.UPDATE_METADATA
        created_at = datetime.now(UTC)
        patch_id = _stable_id(
            "patch", thread_id, proposal.proposal_id, *evidence_ids, str(base_version)
        )
        event_id = decision.existing_event_id or _stable_id("event", proposal.proposal_id)
        claim_payload = [
            item.model_dump(mode="json", exclude={"schema_version"}) for item in claims
        ]
        relation_payload = [
            item.model_dump(mode="json", exclude={"schema_version"}) for item in relations
        ]
        relation_edges = [
            {
                "edge_id": item.relation_id,
                "source_node_kind": "entity",
                "source_node_id": item.source_entity,
                "target_node_kind": "entity",
                "target_node_id": item.target_entity,
                "relation_type": item.relation_type,
                "known_at": item.known_at.isoformat(),
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "evidence_ids": item.evidence_ids,
            }
            for item in relations
        ]
        claim_edges = [
            {
                "edge_id": _stable_id("edge", event_id, item.claim_id, "supports"),
                "source_node_kind": "event",
                "source_node_id": event_id,
                "target_node_kind": "claim",
                "target_node_id": item.claim_id,
                "relation_type": "supports",
                "known_at": item.known_at.isoformat(),
                "valid_from": item.event_time.isoformat(),
                "evidence_ids": item.evidence_ids,
            }
            for item in claims
        ]
        evidence_edges = [
            {
                "edge_id": _stable_id("edge", item.claim_id, evidence_id, "supported_by"),
                "source_node_kind": "claim",
                "source_node_id": item.claim_id,
                "target_node_kind": "evidence",
                "target_node_id": evidence_id,
                "relation_type": "supported_by",
                "known_at": item.known_at.isoformat(),
                "valid_from": None,
                "evidence_ids": [evidence_id],
            }
            for item in claims
            for evidence_id in item.evidence_ids
        ]
        event_entity_edges = [
            {
                "edge_id": _stable_id("edge", event_id, item.source_entity, "involves_entity"),
                "source_node_kind": "event",
                "source_node_id": event_id,
                "target_node_kind": "entity",
                "target_node_id": item.source_entity,
                "relation_type": "involves_entity",
                "known_at": item.known_at.isoformat(),
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "evidence_ids": item.evidence_ids,
            }
            for item in relations
        ]
        return WikiPatch(
            patch_id=patch_id,
            thread_id=thread_id,
            operation=operation,
            event_id=event_id,
            base_version=base_version,
            evidence_ids=evidence_ids,
            payload={
                "event": {
                    "event_id": event_id,
                    "event_family": proposal.event_family,
                    "event_subject": proposal.event_subject,
                    "event_title": proposal.event_title,
                    "event_time": proposal.event_time.isoformat(),
                    "known_at": proposal.known_at.isoformat(),
                    "primary_symbols": proposal.primary_symbols,
                    "evidence_ids": evidence_ids,
                },
                "proposals": _dump([proposal]),
                "decisions": _dump([decision]),
                "claims": claim_payload,
                "relations": relation_payload,
                "edges": [
                    *claim_edges,
                    *evidence_edges,
                    *event_entity_edges,
                    *relation_edges,
                ],
            },
            audit=audit,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            created_at=created_at,
        )
