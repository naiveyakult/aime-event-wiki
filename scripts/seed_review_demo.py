"""Seed one entirely synthetic pending patch for local review-UI testing."""

from datetime import UTC, datetime

from event_wiki.config import Settings
from event_wiki.db import Repository
from event_wiki.models import (
    AuditResult,
    AuditStatus,
    EventFamily,
    EvidenceDocument,
    WikiOperation,
    WikiPatch,
)


def main() -> None:
    now = datetime(2025, 11, 3, 21, 0, tzinfo=UTC)
    repository = Repository.from_url(Settings().database_url, create_schema=True)
    repository.upsert_evidence(
        EvidenceDocument(
            evidence_id="SYNTHETIC_REVIEW_DOC",
            content_type="US_NEWS",
            title="Example Corp announces a synthetic partnership",
            body="Example Corp announced a synthetic partnership with Sample Cloud.",
            published_at=now,
            known_at=now,
            source_name="Synthetic Wire",
            symbols=["EXM"],
            entity_names=["Example Corp", "Sample Cloud"],
            source_locator="synthetic://review-demo",
            content_hash="d" * 64,
        )
    )
    if repository.get_patch("SYNTHETIC_REVIEW_PATCH") is None:
        repository.create_patch(
            WikiPatch(
                patch_id="SYNTHETIC_REVIEW_PATCH",
                thread_id="SYNTHETIC_REVIEW_THREAD",
                operation=WikiOperation.CREATE_EVENT,
                event_id="SYNTHETIC_REVIEW_EVENT",
                base_version=0,
                evidence_ids=["SYNTHETIC_REVIEW_DOC"],
                payload={
                    "event": {
                        "event_id": "SYNTHETIC_REVIEW_EVENT",
                        "event_family": EventFamily.PRODUCT_PARTNERSHIP,
                        "event_subject": "Example Corp",
                        "event_title": "Example Corp synthetic partnership",
                        "event_time": now.isoformat(),
                        "known_at": now.isoformat(),
                    },
                    "claims": [],
                    "relations": [],
                    "edges": [],
                },
                audit=AuditResult(status=AuditStatus.PASS),
                model_version="synthetic-demo",
                prompt_version="v1",
            )
        )
    repository.close()


if __name__ == "__main__":
    main()
