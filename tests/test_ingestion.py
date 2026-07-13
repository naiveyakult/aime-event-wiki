import json
from datetime import UTC, datetime
from pathlib import Path

from event_wiki.ingestion import (
    DateWindow,
    extract_accession,
    iter_v1_evidence,
    latest_complete_month,
    restore_notice_bodies,
)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_ingestion_filters_noise_and_is_streaming(tmp_path: Path) -> None:
    source = tmp_path / "news.input"
    write_jsonl(
        source,
        [
            {
                "id": "keep",
                "content_type": "US_NEWS",
                "title": "Example launches a new medical device",
                "body": "Example announced the product at its official event.",
                "published_at": "2025-11-03T21:00:00Z",
                "source": {"name": "Synthetic Wire", "url": "synthetic://keep"},
                "entities": {"stock": [{"code": "EXM", "name": "Example"}]},
            },
            {
                "id": "crypto",
                "content_type": "US_NEWS",
                "title": "Bitcoin price leaderboard",
                "body": "Daily crypto movers and price ranking.",
                "published_at": "2025-11-03T21:00:00Z",
                "tags": ["crypto", "Top Gainers"],
            },
        ],
    )
    window = DateWindow(
        start=datetime(2025, 11, 1, tzinfo=UTC),
        end=datetime(2025, 12, 1, tzinfo=UTC),
    )
    docs = list(iter_v1_evidence(source, window))
    assert [doc.evidence_id for doc in docs] == ["keep"]
    assert docs[0].symbols == ["EXM"]


def test_notice_accession_and_offline_body_restore(tmp_path: Path) -> None:
    accession = "000123456725000001"
    assert extract_accession(f"notice:{accession}") == accession
    notice_source = tmp_path / "notice.input"
    write_jsonl(
        notice_source,
        [
            {
                "id": "p2",
                "doc_id": "D1",
                "source_type": "notice",
                "title": "EXM Form 8-K",
                "text": "Second paragraph.",
                "paragraph_index": 2,
                "published_at": "2025-11-03T21:00:00Z",
                "source": {
                    "url": "https://sec.example/Archives/edgar/data/1/000123456725000001/a.htm"
                },
            },
            {
                "id": "p1",
                "doc_id": "D1",
                "source_type": "notice",
                "title": "EXM Form 8-K",
                "text": "First paragraph.",
                "paragraph_index": 1,
                "published_at": "2025-11-03T21:00:00Z",
                "source": {
                    "url": "https://sec.example/Archives/edgar/data/1/000123456725000001/a.htm"
                },
            },
        ],
    )
    restored = restore_notice_bodies(notice_source, {accession})
    assert restored[(accession, "D1")].body == "First paragraph.\n\nSecond paragraph."


def test_notice_restore_keeps_documents_with_same_accession_separate(tmp_path: Path) -> None:
    accession = "000123456725000001"
    notice_source = tmp_path / "notice.input"
    rows = []
    for doc_id, text in (("D1", "Primary filing."), ("D2", "Exhibit filing.")):
        rows.append(
            {
                "id": f"{doc_id}-p1",
                "doc_id": doc_id,
                "title": f"{doc_id} Form 8-K",
                "text": text,
                "paragraph_index": 1,
                "published_at": "2025-11-03T21:00:00Z",
                "source": {"url": f"https://sec.example/{accession}/{doc_id}.htm"},
            }
        )
    write_jsonl(notice_source, rows)

    restored = restore_notice_bodies(notice_source, {accession})

    assert set(restored) == {(accession, "D1"), (accession, "D2")}
    assert {item.body for item in restored.values()} == {"Primary filing.", "Exhibit filing."}


def test_latest_complete_month_uses_month_before_maximum_timestamp(tmp_path: Path) -> None:
    source = tmp_path / "news.input"
    write_jsonl(
        source,
        [
            {"published_at": "2025-10-02T00:00:00Z"},
            {"published_at": "2025-12-30T23:59:59Z"},
        ],
    )
    window = latest_complete_month([source])
    assert window.start == datetime(2025, 11, 1, tzinfo=UTC)
    assert window.end == datetime(2025, 12, 1, tzinfo=UTC)
