from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from event_wiki.models import (
    AuditIssue,
    AuditResult,
    AuditStatus,
    Claim,
    EvidenceDocument,
    Relation,
)


def _normalized(value: str) -> str:
    return " ".join(value.lower().split())


_NUMBER_PATTERN = re.compile(
    r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|trillion)?\b",
    re.IGNORECASE,
)
_SCALE_MULTIPLIERS = {
    "thousand": Decimal(1_000),
    "million": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
    "trillion": Decimal(1_000_000_000_000),
}


def _canonical_number(value: str, scale: str | None) -> str:
    number = Decimal(value.replace(",", ""))
    if scale:
        number *= _SCALE_MULTIPLIERS[scale.lower()]
    canonical = format(number, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical


def _numbers(value: str) -> set[str]:
    return {
        _canonical_number(number, scale or None) for number, scale in _NUMBER_PATTERN.findall(value)
    }


def _mentions(document: EvidenceDocument, entity: str) -> bool:
    needle = _normalized(entity)
    values = [document.title, document.body, *document.entity_names, *document.symbols]
    return any(needle == _normalized(value) or needle in _normalized(value) for value in values)


def audit_knowledge(
    claims: list[Claim],
    relations: list[Relation],
    evidence: dict[str, EvidenceDocument],
    prediction_cutoff: datetime,
) -> AuditResult:
    issues: list[AuditIssue] = []
    for index, claim in enumerate(claims):
        for evidence_id in claim.evidence_ids:
            document = evidence.get(evidence_id)
            if document is None:
                issues.append(
                    AuditIssue(
                        code="missing_evidence",
                        message="Claim evidence is missing",
                        severity="block",
                        field_ref=f"claims.{index}",
                        evidence_id=evidence_id,
                    )
                )
                continue
            if _normalized(claim.quote) not in _normalized(f"{document.title} {document.body}"):
                issues.append(
                    AuditIssue(
                        code="unsupported_quote",
                        message="Quoted text is not present in its evidence",
                        severity="block",
                        field_ref=f"claims.{index}.quote",
                        evidence_id=evidence_id,
                    )
                )
            if document.known_at > claim.known_at:
                issues.append(
                    AuditIssue(
                        code="claim_predates_evidence",
                        message="Claim known_at is earlier than its evidence",
                        severity="block",
                        field_ref=f"claims.{index}.known_at",
                        evidence_id=evidence_id,
                    )
                )
        unsupported_numbers = _numbers(claim.object_value) - _numbers(claim.quote)
        if unsupported_numbers:
            issues.append(
                AuditIssue(
                    code="unsupported_number",
                    message="Claim value contains numbers absent from the supporting quote",
                    severity="block",
                    field_ref=f"claims.{index}.object_value",
                )
            )
        if claim.known_at > prediction_cutoff:
            issues.append(
                AuditIssue(
                    code="future_information",
                    message="Claim was not known at the prediction cutoff",
                    severity="block",
                    field_ref=f"claims.{index}.known_at",
                )
            )
    for index, relation in enumerate(relations):
        if relation.known_at > prediction_cutoff:
            issues.append(
                AuditIssue(
                    code="future_information",
                    message="Relation was not known at the prediction cutoff",
                    severity="block",
                    field_ref=f"relations.{index}.known_at",
                )
            )
        for evidence_id in relation.evidence_ids:
            document = evidence.get(evidence_id)
            if document is None:
                issues.append(
                    AuditIssue(
                        code="missing_evidence",
                        message="Relation evidence is missing",
                        severity="block",
                        field_ref=f"relations.{index}",
                        evidence_id=evidence_id,
                    )
                )
                continue
            if document.known_at > relation.known_at:
                issues.append(
                    AuditIssue(
                        code="relation_predates_evidence",
                        message="Relation known_at is earlier than its evidence",
                        severity="block",
                        field_ref=f"relations.{index}.known_at",
                        evidence_id=evidence_id,
                    )
                )
        documents = [evidence[item] for item in relation.evidence_ids if item in evidence]
        for field, entity in (
            ("source_entity", relation.source_entity),
            ("target_entity", relation.target_entity),
        ):
            if documents and not any(_mentions(document, entity) for document in documents):
                issues.append(
                    AuditIssue(
                        code="unsupported_relation_endpoint",
                        message=f"Relation {field} is absent from its evidence",
                        severity="block",
                        field_ref=f"relations.{index}.{field}",
                    )
                )
    status = (
        AuditStatus.BLOCK
        if any(issue.severity == "block" for issue in issues)
        else (AuditStatus.WARN if issues else AuditStatus.PASS)
    )
    return AuditResult(status=status, issues=issues)
