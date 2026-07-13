from __future__ import annotations

from datetime import UTC, datetime, timedelta

from event_wiki.evaluation import GoldEvent
from event_wiki.models import EventFamily, EvidenceDocument

FAMILIES = [
    EventFamily.EARNINGS,
    EventFamily.REGULATORY,
    EventFamily.PRODUCT_PARTNERSHIP,
]


def build_synthetic_gold() -> tuple[list[EvidenceDocument], list[GoldEvent]]:
    """Build 200 invented documents containing 30 independently labeled events."""
    base = datetime(2025, 11, 1, 14, 0, tzinfo=UTC)
    documents: list[EvidenceDocument] = []
    gold: list[GoldEvent] = []
    for index in range(30):
        family = FAMILIES[index % len(FAMILIES)]
        ids: list[str] = []
        for report in range(3):
            evidence_id = f"SYN_EVENT_{index:02d}_{report}"
            ids.append(evidence_id)
            timestamp = base + timedelta(hours=index * 12 + report)
            action = {
                EventFamily.EARNINGS: "reported synthetic quarterly earnings",
                EventFamily.REGULATORY: "received a synthetic regulatory order",
                EventFamily.PRODUCT_PARTNERSHIP: "announced a synthetic product partnership",
            }[family]
            body = f"Example Company {index} {action}."
            documents.append(
                EvidenceDocument(
                    evidence_id=evidence_id,
                    content_type="US_NEWS",
                    title=body,
                    body=body,
                    published_at=timestamp,
                    known_at=timestamp,
                    source_name="Synthetic Wire",
                    symbols=[f"X{index:02d}"],
                    entity_names=[f"Example Company {index}"],
                    source_locator=f"synthetic://event/{index}/{report}",
                    content_hash=f"{index * 3 + report:064x}",
                )
            )
        gold.append(GoldEvent(f"GOLD_{index:02d}", family, frozenset(ids)))
    for index in range(110):
        timestamp = base + timedelta(minutes=index)
        body = f"Synthetic market commentary number {index} with no discrete event."
        documents.append(
            EvidenceDocument(
                evidence_id=f"SYN_NOISE_{index:03d}",
                content_type="US_NEWS",
                title=body,
                body=body,
                published_at=timestamp,
                known_at=timestamp,
                source_name="Synthetic Wire",
                source_locator=f"synthetic://noise/{index}",
                content_hash=f"{10_000 + index:064x}",
            )
        )
    return documents, gold
