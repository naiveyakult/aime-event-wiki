import json
from datetime import UTC, datetime
from pathlib import Path

from event_wiki.ingestion import DateWindow
from event_wiki.pipeline import generate_candidates, ingest_sources


class FakeRepository:
    def __init__(self) -> None:
        self.documents = {}

    def upsert_evidence(self, document) -> None:
        self.documents[document.evidence_id] = document


class PagedCandidateRepository:
    def __init__(self, documents: list, page_size: int = 97) -> None:
        self.documents = documents
        self.page_size = page_size
        self.candidates = {}
        self.iterated = False

    def list_evidence(self, **_kwargs):
        raise AssertionError("generate_candidates must not materialize all evidence")

    def iter_evidence_pages(self, **_kwargs):
        self.iterated = True
        for offset in range(0, len(self.documents), self.page_size):
            yield self.documents[offset : offset + self.page_size]

    def save_candidate(self, candidate) -> None:
        self.candidates[candidate.candidate_id] = candidate


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_ingest_sources_writes_manifest_and_restored_notice(tmp_path: Path) -> None:
    root = tmp_path / "data"
    write_jsonl(
        root / "v1" / "US_NEWS.jsonl",
        [
            {
                "id": "NEWS_1",
                "content_type": "US_NEWS",
                "title": "Example reports quarterly earnings",
                "body": "Example reported synthetic quarterly earnings.",
                "published_at": "2025-11-03T21:00:00Z",
                "source": {"name": "Synthetic Wire"},
            }
        ],
    )
    write_jsonl(root / "v1" / "US_FLASH.jsonl", [])
    write_jsonl(
        root / "v1" / "US_NOTICE.jsonl",
        [
            {
                "id": "NOTICE_1",
                "content_type": "US_NOTICE",
                "title": "Form 8-K - Current report",
                "published_at": "2025-11-03T21:00:00Z",
                "dedup": {"key": "notice:000123456725000001"},
            }
        ],
    )
    write_jsonl(
        root / "v2" / "notice.jsonl",
        [
            {
                "id": "P1",
                "doc_id": "D1",
                "source_type": "notice",
                "title": "EXM Form 8-K",
                "text": "Example entered a synthetic partnership.",
                "paragraph_index": 1,
                "published_at": "2025-11-03T21:00:00Z",
                "source": {"url": "https://sec.example/000123456725000001/a.htm"},
            }
        ],
    )
    repo = FakeRepository()
    manifest = ingest_sources(
        repo,
        root,
        DateWindow(datetime(2025, 11, 1, tzinfo=UTC), datetime(2025, 12, 1, tzinfo=UTC)),
        tmp_path / "out",
        hash_inputs=False,
    )
    assert manifest.documents_written == 3
    assert "V2_NOTICE:000123456725000001:D1" in repo.documents
    assert (tmp_path / "out" / f"ingestion_{manifest.run_id}.json").exists()


def test_ingest_sources_restores_each_notice_document(tmp_path: Path) -> None:
    root = tmp_path / "data"
    write_jsonl(root / "v1" / "US_NEWS.jsonl", [])
    write_jsonl(root / "v1" / "US_FLASH.jsonl", [])
    accession = "000123456725000001"
    write_jsonl(
        root / "v1" / "US_NOTICE.jsonl",
        [
            {
                "id": "NOTICE_1",
                "content_type": "US_NOTICE",
                "title": "Form 8-K - Current report",
                "published_at": "2025-11-03T21:00:00Z",
                "dedup": {"key": f"notice:{accession}"},
            }
        ],
    )
    write_jsonl(
        root / "v2" / "notice.jsonl",
        [
            {
                "id": f"P-{doc_id}",
                "doc_id": doc_id,
                "title": "EXM Form 8-K",
                "text": text,
                "paragraph_index": 1,
                "published_at": "2025-11-03T21:00:00Z",
                "source": {"url": f"https://sec.example/{accession}/{doc_id}.htm"},
            }
            for doc_id, text in (("D1", "Primary."), ("D2", "Exhibit."))
        ],
    )
    repo = FakeRepository()

    manifest = ingest_sources(
        repo,
        root,
        DateWindow(datetime(2025, 11, 1, tzinfo=UTC), datetime(2025, 12, 1, tzinfo=UTC)),
        tmp_path / "out",
        hash_inputs=False,
    )

    assert manifest.counts_by_type["V2_NOTICE"] == 2
    assert {key for key in repo.documents if key.startswith("V2_NOTICE:")} == {
        f"V2_NOTICE:{accession}:D1",
        f"V2_NOTICE:{accession}:D2",
    }


def test_generate_candidates_streams_pages_and_covers_thousands_of_documents() -> None:
    from datetime import timedelta

    from event_wiki.models import EvidenceDocument

    base = datetime(2025, 11, 1, tzinfo=UTC)
    documents = []
    for index in range(4_000):
        published = base + timedelta(minutes=index * 10)
        documents.append(
            EvidenceDocument(
                evidence_id=f"E{index:05d}",
                content_type="US_NEWS",
                title=f"Issuer {index % 100} announces update topic {index % 17}",
                body="Synthetic evidence only.",
                published_at=published,
                known_at=published,
                source_name="Synthetic Wire",
                symbols=[f"S{index % 100}"],
                source_locator=f"synthetic://{index}",
                content_hash=f"{index:064x}",
            )
        )
    repository = PagedCandidateRepository(documents)

    count = generate_candidates(repository)

    covered = [
        evidence_id
        for candidate in repository.candidates.values()
        for evidence_id in candidate.evidence_ids
    ]
    assert repository.iterated
    assert count == len(repository.candidates)
    assert sorted(covered) == sorted(item.evidence_id for item in documents)
    assert len(covered) == len(set(covered))

    differently_paged = PagedCandidateRepository(documents, page_size=251)
    generate_candidates(differently_paged)
    assert set(differently_paged.candidates) == set(repository.candidates)
