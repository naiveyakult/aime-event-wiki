from __future__ import annotations

import re
from datetime import UTC
from typing import Any

from event_wiki.models import (
    AuditIssue,
    AuditStatus,
    EventLink,
    EventLinkAuditResult,
    EventLinkType,
    EvidenceDocument,
)

BATCH_TYPES = {
    EventLinkType.SAME_DRIVER,
    EventLinkType.EVIDENCE_UPDATE,
    EventLinkType.TEMPORAL_SEQUENCE,
}


def _identity_tokens(event: dict[str, Any]) -> set[str]:
    text = " ".join(
        [str(event.get("event_subject", "")), *event.get("primary_symbols", [])]
    ).lower()
    return {token for token in re.findall(r"[a-z0-9]+", text) if len(token) >= 3}


def audit_event_link(
    link: EventLink,
    source: dict[str, Any] | None,
    target: dict[str, Any] | None,
    evidence: dict[str, EvidenceDocument],
) -> EventLinkAuditResult:
    issues: list[AuditIssue] = []
    if source is None or target is None:
        issues.append(
            AuditIssue(code="missing_event_endpoint", message="事件端点不存在", severity="block")
        )
    elif (
        source.get("event_id") != link.source_event_id
        or target.get("event_id") != link.target_event_id
    ):
        issues.append(
            AuditIssue(
                code="foreign_event_endpoint", message="事件端点不在候选中", severity="block"
            )
        )

    quote_ids: set[str] = set()
    quotes_valid = True
    for quote in link.evidence_quotes:
        quote_ids.add(quote.evidence_id)
        document = evidence.get(quote.evidence_id)
        if document is None:
            quotes_valid = False
            issues.append(
                AuditIssue(
                    code="missing_link_evidence",
                    message="链接引用的 Evidence 不存在",
                    severity="block",
                    evidence_id=quote.evidence_id,
                )
            )
            continue
        if quote.quote not in f"{document.title} {document.body}":
            quotes_valid = False
            issues.append(
                AuditIssue(
                    code="unsupported_link_quote",
                    message="链接引用无法在 Evidence 中逐字定位",
                    severity="block",
                    evidence_id=quote.evidence_id,
                )
            )
        if document.known_at > link.known_at.astimezone(UTC):
            issues.append(
                AuditIssue(
                    code="future_link_evidence",
                    message="链接使用了 known_at 之后的 Evidence",
                    severity="block",
                    evidence_id=quote.evidence_id,
                )
            )

    if issues:
        return EventLinkAuditResult(status=AuditStatus.BLOCK, issues=issues, disposition="blocked")
    if link.confidence < 0.65:
        return EventLinkAuditResult(status=AuditStatus.WARN, issues=[], disposition="suppressed")

    source_evidence = set(source.get("evidence_ids", [])) if source else set()
    target_evidence = set(target.get("evidence_ids", [])) if target else set()
    shared = source_evidence & target_evidence & quote_ids
    combined_quote_tokens = set(
        re.findall(r"[a-z0-9]+", " ".join(item.quote for item in link.evidence_quotes).lower())
    )
    source_named = bool(_identity_tokens(source or {}) & combined_quote_tokens)
    target_named = bool(_identity_tokens(target or {}) & combined_quote_tokens)
    if (
        not link.inferred
        and link.confidence >= 0.9
        and shared
        and quotes_valid
        and source_named
        and target_named
    ):
        return EventLinkAuditResult(status=AuditStatus.PASS, issues=[], disposition="auto_commit")
    if link.inferred and link.link_type in BATCH_TYPES and link.confidence >= 0.85:
        return EventLinkAuditResult(status=AuditStatus.PASS, issues=[], disposition="batch_review")
    return EventLinkAuditResult(
        status=AuditStatus.WARN if link.inferred else AuditStatus.PASS,
        issues=[],
        disposition="individual_review",
    )
