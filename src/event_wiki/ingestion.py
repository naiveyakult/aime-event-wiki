from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from event_wiki.models import EvidenceDocument

NOISE_TAGS = {
    "crypto",
    "technical analysis",
    "top gainers",
    "top losers",
    "today_mover",
    "personal_finance",
}
NOISE_PHRASES = (
    "daily turnover",
    "price leaderboard",
    "technical analysis",
    "market recap",
    "top gainers",
    "top losers",
)
NOTICE_FORMS = ("8-K", "6-K", "10-Q", "10-K")
ACCESSION_RE = re.compile(r"(?<!\d)(\d{18})(?!\d)")


@dataclass(frozen=True)
class DateWindow:
    start: datetime
    end: datetime

    def contains(self, value: datetime) -> bool:
        return self.start <= value < self.end


@dataclass(frozen=True)
class RestoredNotice:
    accession: str
    doc_id: str
    title: str
    body: str
    published_at: datetime
    source_url: str | None


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def latest_complete_month(paths: list[Path]) -> DateWindow:
    """Return the full calendar month immediately before the latest observed timestamp."""
    latest: datetime | None = None
    for path in paths:
        for _, row in iter_jsonl(path):
            raw = row.get("published_at")
            if not raw:
                continue
            try:
                value = parse_timestamp(raw)
            except (TypeError, ValueError):
                continue
            latest = value if latest is None or value > latest else latest
    if latest is None:
        raise ValueError("no valid published_at timestamp found")
    current_month = datetime(latest.year, latest.month, 1, tzinfo=UTC)
    if current_month.month == 1:
        start = datetime(current_month.year - 1, 12, 1, tzinfo=UTC)
    else:
        start = datetime(current_month.year, current_month.month - 1, 1, tzinfo=UTC)
    return DateWindow(start=start, end=current_month)


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict]]:
    with path.open("rb") as handle:
        while True:
            offset = handle.tell()
            line = handle.readline()
            if not line:
                return
            try:
                yield offset, json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue


def extract_accession(value: str | None) -> str | None:
    if not value:
        return None
    match = ACCESSION_RE.search(value.replace("-", ""))
    return match.group(1) if match else None


def _entities(record: dict) -> tuple[list[str], list[str]]:
    symbols: set[str] = set()
    names: set[str] = set()
    for group, values in (record.get("entities") or {}).items():
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            code = str(value.get("code") or "").upper()
            name = str(value.get("name") or "").strip()
            if group in {"stock", "stocks", "etf"} and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,7}", code):
                symbols.add(code)
            if name:
                names.add(name)
    return sorted(symbols), sorted(names)


def _is_noise(record: dict) -> bool:
    tags = {str(tag).lower() for tag in record.get("tags") or []}
    text = f"{record.get('title') or ''} {record.get('body') or ''}".lower()
    if tags & NOISE_TAGS:
        return True
    return any(phrase in text for phrase in NOISE_PHRASES)


def _notice_allowed(title: str) -> bool:
    upper = title.upper()
    return any(re.search(rf"\b{re.escape(form)}\b", upper) for form in NOTICE_FORMS)


def iter_v1_evidence(path: Path, window: DateWindow) -> Iterator[EvidenceDocument]:
    for offset, record in iter_jsonl(path):
        raw_ts = record.get("published_at")
        if not raw_ts:
            continue
        published_at = parse_timestamp(raw_ts)
        if not window.contains(published_at):
            continue
        content_type = record.get("content_type")
        if content_type not in {"US_NEWS", "US_FLASH", "US_NOTICE"}:
            continue
        title = str(record.get("title") or "").strip()
        body = str(record.get("body") or "").strip()
        if not title or (content_type != "US_NOTICE" and not body):
            continue
        if content_type == "US_NOTICE":
            if not _notice_allowed(title):
                continue
        elif _is_noise(record):
            continue
        source = record.get("source") or {}
        symbols, names = _entities(record)
        accession = extract_accession((record.get("dedup") or {}).get("key"))
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
        yield EvidenceDocument(
            evidence_id=str(record.get("id")),
            content_type=content_type,
            title=title,
            body=body,
            published_at=published_at,
            known_at=published_at,
            source_name=str(source.get("name") or ""),
            source_url=source.get("url"),
            symbols=symbols,
            entity_names=names,
            accession=accession,
            source_locator=f"{path.name}:{offset}",
            content_hash=hashlib.sha256(canonical).hexdigest(),
        )


def restore_notice_bodies(
    path: Path, accessions: set[str]
) -> dict[tuple[str, str], RestoredNotice]:
    grouped: dict[tuple[str, str], list[tuple[int, dict]]] = defaultdict(list)
    for _, row in iter_jsonl(path):
        source_url = (row.get("source") or {}).get("url")
        accession = extract_accession(source_url)
        if accession in accessions:
            doc_id = str(row.get("doc_id") or "")
            grouped[(accession, doc_id)].append((int(row.get("paragraph_index") or 0), row))
    restored: dict[tuple[str, str], RestoredNotice] = {}
    for (accession, doc_id), rows in grouped.items():
        rows.sort(key=lambda item: item[0])
        first = rows[0][1]
        body = "\n\n".join(str(row.get("text") or "").strip() for _, row in rows if row.get("text"))
        restored[(accession, doc_id)] = RestoredNotice(
            accession=accession,
            doc_id=doc_id,
            title=str(first.get("title") or "SEC filing"),
            body=body,
            published_at=parse_timestamp(first["published_at"]),
            source_url=(first.get("source") or {}).get("url"),
        )
    return restored
