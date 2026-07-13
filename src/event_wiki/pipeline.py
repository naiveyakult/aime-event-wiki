from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from event_wiki.ingestion import DateWindow, iter_v1_evidence, restore_notice_bodies
from event_wiki.models import EvidenceDocument
from event_wiki.retrieval import iter_candidate_bundles


class IngestionRepository(Protocol):
    def upsert_evidence(self, document: EvidenceDocument) -> None: ...


class IngestionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    window_start: datetime
    window_end: datetime
    documents_written: int
    counts_by_type: dict[str, int]
    input_files: list[dict]
    created_at: datetime


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ingest_sources(
    repository: IngestionRepository,
    data_root: Path,
    window: DateWindow,
    output_dir: Path,
    *,
    hash_inputs: bool = True,
) -> IngestionManifest:
    v1_paths = [
        data_root / "v1" / name for name in ("US_NEWS.jsonl", "US_FLASH.jsonl", "US_NOTICE.jsonl")
    ]
    missing = [str(path) for path in v1_paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"missing source files: {', '.join(missing)}")

    counts: dict[str, int] = {}
    accessions: set[str] = set()
    input_files: list[dict] = []
    seed_parts = [window.start.isoformat(), window.end.isoformat()]
    for path in v1_paths:
        stat = path.stat()
        metadata = {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        if hash_inputs:
            metadata["sha256"] = _sha256(path)
        input_files.append(metadata)
        seed_parts.extend((str(path), str(stat.st_size), str(stat.st_mtime_ns)))
        for document in iter_v1_evidence(path, window):
            repository.upsert_evidence(document)
            counts[document.content_type] = counts.get(document.content_type, 0) + 1
            if document.accession:
                accessions.add(document.accession)

    v2_path = data_root / "v2" / "notice.jsonl"
    if accessions and v2_path.exists():
        stat = v2_path.stat()
        metadata = {"path": str(v2_path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        if hash_inputs:
            metadata["sha256"] = _sha256(v2_path)
        input_files.append(metadata)
        seed_parts.extend((str(v2_path), str(stat.st_size), str(stat.st_mtime_ns)))
        for restored in restore_notice_bodies(v2_path, accessions).values():
            body_hash = hashlib.sha256(restored.body.encode()).hexdigest()
            document = EvidenceDocument(
                evidence_id=f"V2_NOTICE:{restored.accession}:{restored.doc_id}",
                content_type="V2_NOTICE",
                title=restored.title,
                body=restored.body,
                published_at=restored.published_at,
                known_at=restored.published_at,
                source_name="SEC notice",
                source_url=restored.source_url,
                accession=restored.accession,
                source_locator=(
                    f"{v2_path.name}:accession={restored.accession}:doc_id={restored.doc_id}"
                ),
                content_hash=body_hash,
            )
            repository.upsert_evidence(document)
            counts[document.content_type] = counts.get(document.content_type, 0) + 1

    run_id = hashlib.sha256("\n".join(seed_parts).encode()).hexdigest()[:16]
    manifest = IngestionManifest(
        run_id=run_id,
        window_start=window.start,
        window_end=window.end,
        documents_written=sum(counts.values()),
        counts_by_type=counts,
        input_files=input_files,
        created_at=datetime.now(UTC),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"ingestion_{run_id}.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2)
    )
    return manifest


def generate_candidates(
    repository,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
) -> int:
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return 0
    page_size = min(1_000, limit) if limit is not None else 1_000
    pages = repository.iter_evidence_pages(start=start, end=end, page_size=page_size)
    documents = (document for page in pages for document in page)
    if limit is not None:
        documents = islice(documents, limit)
    count = 0
    for bundle in iter_candidate_bundles(documents):
        repository.save_candidate(bundle)
        count += 1
    return count
