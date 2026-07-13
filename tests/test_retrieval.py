import hashlib
from datetime import UTC, datetime, timedelta

from event_wiki.models import EvidenceDocument
from event_wiki.retrieval import RetrievalStats, build_candidate_bundles, candidate_id_for

BASE = datetime(2025, 11, 3, 21, 0, tzinfo=UTC)


def doc(doc_id: str, title: str, offset: int, symbol: str = "EXM") -> EvidenceDocument:
    ts = BASE + timedelta(days=offset)
    return EvidenceDocument(
        evidence_id=doc_id,
        content_type="US_NEWS",
        title=title,
        body=f"Synthetic evidence for {title}",
        published_at=ts,
        known_at=ts,
        source_name="Synthetic Wire",
        symbols=[symbol],
        source_locator=f"synthetic://{doc_id}",
        content_hash=hashlib.sha256(doc_id.encode()).hexdigest(),
    )


def test_candidate_id_is_order_independent() -> None:
    assert candidate_id_for(["B", "A"]) == candidate_id_for(["A", "B"])


def test_retrieval_groups_related_docs_but_not_distant_event() -> None:
    docs = [
        doc("A", "Example reports quarterly earnings and revenue", 0),
        doc("B", "Example quarterly earnings revenue beats estimate", 1),
        doc("C", "Example launches unrelated device", 10),
    ]
    bundles = build_candidate_bundles(docs)
    evidence_sets = [set(bundle.evidence_ids) for bundle in bundles]
    assert {"A", "B"} in evidence_sets
    assert {"C"} in evidence_sets


def test_large_connected_group_is_chunked_without_dropping_evidence() -> None:
    docs = [doc(f"D{i:03d}", "Example reports quarterly earnings revenue", 0) for i in range(35)]

    bundles = build_candidate_bundles(docs, max_documents=12)

    flattened = [evidence_id for bundle in bundles for evidence_id in bundle.evidence_ids]
    assert len(bundles) == 3
    assert sorted(flattened) == sorted(item.evidence_id for item in docs)
    assert len(flattened) == len(set(flattened))
    assert all(len(bundle.evidence_ids) <= 12 for bundle in bundles)


def test_hot_symbol_bucket_has_bounded_comparisons_and_full_coverage() -> None:
    docs = [
        doc(f"HOT{i:04d}", f"Example update topic{i % 40} detail{i}", i // 150, symbol="HOT")
        for i in range(3_000)
    ]
    stats = RetrievalStats()

    bundles = build_candidate_bundles(docs, max_comparisons_per_document=32, stats=stats)

    flattened = [evidence_id for bundle in bundles for evidence_id in bundle.evidence_ids]
    assert sorted(flattened) == sorted(item.evidence_id for item in docs)
    assert len(flattened) == len(set(flattened))
    assert stats.title_comparisons <= len(docs) * 32
    assert stats.index_candidates_scanned <= len(docs) * 32 * 4
