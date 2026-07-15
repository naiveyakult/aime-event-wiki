import hashlib
from datetime import UTC, datetime, timedelta

from event_wiki.models import EvidenceDocument
from event_wiki.retrieval import RetrievalStats, build_candidate_bundles, candidate_id_for

BASE = datetime(2025, 11, 3, 21, 0, tzinfo=UTC)


def doc(
    doc_id: str,
    title: str,
    offset: int,
    symbol: str = "EXM",
    entity_names: list[str] | None = None,
) -> EvidenceDocument:
    ts = BASE + timedelta(days=offset)
    return EvidenceDocument(
        evidence_id=doc_id,
        content_type="US_NEWS",
        title=title,
        body=f"Synthetic evidence for {title}",
        published_at=ts,
        known_at=ts,
        source_name="Synthetic Wire",
        symbols=[symbol] if symbol else [],
        entity_names=entity_names or [],
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


def test_sec_boilerplate_does_not_merge_unrelated_issuers_without_symbols() -> None:
    docs = [
        doc(
            "QLYS",
            "QLYS Form 10-Q - Q3 2025 Earnings Report",
            0,
            symbol="",
            entity_names=["Qualys"],
        ),
        doc(
            "EXC",
            "EXC Form 10-Q - Q3 2025 Earnings Report",
            0,
            symbol="",
            entity_names=["Exelon"],
        ),
    ]

    bundles = build_candidate_bundles(docs)

    assert {frozenset(bundle.evidence_ids) for bundle in bundles} == {
        frozenset({"QLYS"}),
        frozenset({"EXC"}),
    }


def test_sec_boilerplate_without_identity_metadata_stays_separate() -> None:
    docs = [
        doc("QLYS", "QLYS Form 10-Q - Q3 2025 Earnings Report", 0, symbol=""),
        doc("EXC", "EXC Form 10-Q - Q3 2025 Earnings Report", 0, symbol=""),
    ]

    bundles = build_candidate_bundles(docs)

    assert {frozenset(bundle.evidence_ids) for bundle in bundles} == {
        frozenset({"QLYS"}),
        frozenset({"EXC"}),
    }


def test_raw_notice_accessions_do_not_form_generic_filing_clusters() -> None:
    docs = [
        doc(
            "NOTICE_A",
            "Form 8-K - Current report:_0001193125-25-291330",
            0,
            symbol="",
        ),
        doc(
            "NOTICE_B",
            "Form 8-K - Current report:_0001193125-25-291325",
            0,
            symbol="",
        ),
    ]

    bundles = build_candidate_bundles(docs)

    assert {frozenset(bundle.evidence_ids) for bundle in bundles} == {
        frozenset({"NOTICE_A"}),
        frozenset({"NOTICE_B"}),
    }


def test_generic_earnings_metrics_do_not_cluster_different_companies() -> None:
    docs = [
        doc(
            "ALPHA",
            "Alpha Systems Non-GAAP EPS beats estimate, revenue surpasses forecast",
            0,
            symbol="",
        ),
        doc(
            "BETA",
            "Beta Networks Non-GAAP EPS beats estimate, revenue surpasses forecast",
            0,
            symbol="",
        ),
    ]

    bundles = build_candidate_bundles(docs)

    assert {frozenset(bundle.evidence_ids) for bundle in bundles} == {
        frozenset({"ALPHA"}),
        frozenset({"BETA"}),
    }


def test_same_entity_still_groups_related_earnings_evidence_without_symbols() -> None:
    docs = [
        doc(
            "A",
            "Example Corp reports third-quarter revenue growth",
            0,
            symbol="",
            entity_names=["Example Corp"],
        ),
        doc(
            "B",
            "Example Corp third-quarter earnings revenue update",
            1,
            symbol="",
            entity_names=["Example Corp"],
        ),
    ]

    bundles = build_candidate_bundles(docs)

    assert [set(bundle.evidence_ids) for bundle in bundles] == [{"A", "B"}]


def test_transitive_title_bridge_cannot_merge_unrelated_topics() -> None:
    docs = [
        doc(
            "COFFEE",
            "Brazil coffee prices surge after tariff change",
            0,
            symbol="",
            entity_names=["Brazil"],
        ),
        doc(
            "BRIDGE",
            "Brazil coffee prices support investor buying interest after tariff",
            0,
            symbol="",
            entity_names=["Brazil"],
        ),
        doc(
            "STOCK",
            "Investor buying interest lifts technology shares",
            0,
            symbol="",
            entity_names=["Brazil"],
        ),
    ]

    bundles = build_candidate_bundles(docs, title_threshold=0.18)
    evidence_sets = [set(bundle.evidence_ids) for bundle in bundles]

    assert {"COFFEE", "BRIDGE"} in evidence_sets
    assert {"STOCK"} in evidence_sets
