from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    or_,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from event_wiki.audit import audit_knowledge
from event_wiki.models import (
    AuditIssue,
    AuditResult,
    AuditStatus,
    CandidateBundle,
    EventFamily,
    EvidenceDocument,
    PatchPayload,
    WikiOperation,
    WikiPatch,
)


def _utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


class RepositoryError(RuntimeError):
    pass


class VersionConflict(RepositoryError):
    pass


class Base(DeclarativeBase):
    pass


class EvidenceRow(Base):
    __tablename__ = "evidence_documents"

    evidence_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source_name: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str | None] = mapped_column(Text)
    symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    entity_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    accession: Mapped[str | None] = mapped_column(String(32), index=True)
    source_locator: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0")


class CandidateRow(Base):
    __tablename__ = "candidate_bundles"

    candidate_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    entity_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    retrieval_reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0")


class EventRow(Base):
    __tablename__ = "events"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_family: Mapped[str] = mapped_column(String(64), index=True)
    event_subject: Mapped[str] = mapped_column(Text, index=True)
    event_title: Mapped[str] = mapped_column(Text)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EventAliasRow(Base):
    __tablename__ = "event_aliases"

    alias_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(Text, unique=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id"), index=True)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ClaimRow(Base):
    __tablename__ = "claims"

    claim_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id"), index=True)
    subject: Mapped[str] = mapped_column(Text)
    predicate: Mapped[str] = mapped_column(Text)
    object_value: Mapped[str] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(64))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON)
    quote: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    superseded_by: Mapped[str | None] = mapped_column(String(255))


class RelationRow(Base):
    __tablename__ = "relations"

    relation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id"), index=True)
    source_entity: Mapped[str] = mapped_column(Text)
    target_entity: Mapped[str] = mapped_column(Text)
    relation_type: Mapped[str] = mapped_column(String(64), index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON)
    rationale: Mapped[str] = mapped_column(Text)
    inferred_for_event: Mapped[bool] = mapped_column(default=False)
    confidence: Mapped[float] = mapped_column(Float)


class EventEdgeRow(Base):
    __tablename__ = "event_edges"

    edge_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id"), index=True)
    source_node_kind: Mapped[str] = mapped_column(String(32), index=True)
    source_node_id: Mapped[str] = mapped_column(Text, index=True)
    target_node_kind: Mapped[str] = mapped_column(String(32), index=True)
    target_node_id: Mapped[str] = mapped_column(Text, index=True)
    relation_type: Mapped[str] = mapped_column(String(64), index=True)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer)


class WikiPatchRow(Base):
    __tablename__ = "wiki_patches"

    patch_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    operation: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[str | None] = mapped_column(String(255), index=True)
    base_version: Mapped[int] = mapped_column(Integer)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    audit: Mapped[dict[str, Any]] = mapped_column(JSON)
    model_version: Mapped[str] = mapped_column(String(255))
    prompt_version: Mapped[str] = mapped_column(String(255))
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)


class WikiVersionRow(Base):
    __tablename__ = "wiki_versions"
    __table_args__ = (UniqueConstraint("event_id", "version", name="uq_wiki_event_version"),)

    version_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    patch_id: Mapped[str] = mapped_column(ForeignKey("wiki_patches.patch_id"), unique=True)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReviewActionRow(Base):
    __tablename__ = "review_actions"

    review_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patch_id: Mapped[str] = mapped_column(ForeignKey("wiki_patches.patch_id"), index=True)
    decision: Mapped[str] = mapped_column(String(32), index=True)
    reviewer: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(Text, default="")
    edited_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentRunRow(Base):
    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    current_node: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Repository:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, database_url: str, *, create_schema: bool = False) -> Repository:
        engine = create_engine(database_url)
        repository = cls(engine)
        if create_schema:
            Base.metadata.create_all(engine)
        return repository

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._sessions.begin() as session:
            yield session

    def upsert_evidence(self, document: EvidenceDocument) -> None:
        values = document.model_dump(mode="json")
        values["published_at"] = document.published_at
        values["known_at"] = document.known_at
        with self.session() as session:
            row = session.get(EvidenceRow, document.evidence_id)
            if row is None:
                session.add(EvidenceRow(**values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)

    def list_evidence(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        evidence_ids: Sequence[str] | None = None,
    ) -> list[EvidenceDocument]:
        statement = select(EvidenceRow).order_by(EvidenceRow.published_at, EvidenceRow.evidence_id)
        if start is not None:
            statement = statement.where(EvidenceRow.published_at >= start)
        if end is not None:
            statement = statement.where(EvidenceRow.published_at < end)
        if evidence_ids is not None:
            statement = statement.where(EvidenceRow.evidence_id.in_(evidence_ids))
        if limit is not None:
            statement = statement.limit(limit)
        with self.session() as session:
            return [self._evidence_model(row) for row in session.scalars(statement)]

    def _evidence_model(self, row: EvidenceRow) -> EvidenceDocument:
        return EvidenceDocument.model_validate(
            {
                column.name: (
                    _utc(getattr(row, column.name))
                    if column.name in {"published_at", "known_at"}
                    else getattr(row, column.name)
                )
                for column in EvidenceRow.__table__.columns
            }
        )

    def iter_evidence_pages(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        page_size: int = 1000,
    ) -> Iterator[list[EvidenceDocument]]:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        last_time: datetime | None = None
        last_id = ""
        while True:
            statement = select(EvidenceRow).order_by(
                EvidenceRow.published_at, EvidenceRow.evidence_id
            )
            if start is not None:
                statement = statement.where(EvidenceRow.published_at >= start)
            if end is not None:
                statement = statement.where(EvidenceRow.published_at < end)
            if last_time is not None:
                statement = statement.where(
                    or_(
                        EvidenceRow.published_at > last_time,
                        (EvidenceRow.published_at == last_time)
                        & (EvidenceRow.evidence_id > last_id),
                    )
                )
            with self.session() as session:
                rows = list(session.scalars(statement.limit(page_size)))
                page = [self._evidence_model(row) for row in rows]
            if not page:
                return
            yield page
            last_time = page[-1].published_at
            last_id = page[-1].evidence_id

    def save_candidate(self, candidate: CandidateBundle, *, status: str = "pending") -> None:
        values = candidate.model_dump(mode="json")
        values.update(
            window_start=candidate.window_start, window_end=candidate.window_end, status=status
        )
        with self.session() as session:
            row = session.get(CandidateRow, candidate.candidate_id)
            if row is None:
                session.add(CandidateRow(**values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)

    def get_candidate(self, candidate_id: str) -> CandidateBundle | None:
        with self.session() as session:
            row = session.get(CandidateRow, candidate_id)
            return self._candidate_model(row) if row else None

    def list_candidates(
        self, *, status: str | None = None, limit: int | None = None
    ) -> list[CandidateBundle]:
        statement = select(CandidateRow).order_by(
            CandidateRow.window_start, CandidateRow.candidate_id
        )
        if status:
            statement = statement.where(CandidateRow.status == status)
        if limit is not None:
            statement = statement.limit(limit)
        with self.session() as session:
            return [self._candidate_model(row) for row in session.scalars(statement)]

    @staticmethod
    def _candidate_model(row: CandidateRow) -> CandidateBundle:
        return CandidateBundle.model_validate(
            {
                "candidate_id": row.candidate_id,
                "evidence_ids": row.evidence_ids,
                "window_start": _utc(row.window_start),
                "window_end": _utc(row.window_end),
                "symbols": row.symbols,
                "entity_names": row.entity_names,
                "retrieval_reason": row.retrieval_reason,
                "schema_version": row.schema_version,
            }
        )

    def delete_candidate(self, candidate_id: str) -> bool:
        with self.session() as session:
            return bool(
                session.execute(
                    delete(CandidateRow).where(CandidateRow.candidate_id == candidate_id)
                ).rowcount
            )

    def find_existing_events(
        self,
        *,
        subject: str,
        event_family: EventFamily | str,
        around: datetime,
        window_days: int = 2,
    ) -> list[dict[str, Any]]:
        family = event_family.value if isinstance(event_family, EventFamily) else event_family
        statement = (
            select(EventRow)
            .where(
                func.lower(EventRow.event_subject) == subject.lower(),
                EventRow.event_family == family,
                EventRow.event_time >= around - timedelta(days=window_days),
                EventRow.event_time <= around + timedelta(days=window_days),
                EventRow.status == "active",
            )
            .order_by(EventRow.event_time, EventRow.event_id)
        )
        with self.session() as session:
            return [self._event_dict(row) for row in session.scalars(statement)]

    lookup_existing_events = find_existing_events

    def get_event_version(self, event_id: str) -> int:
        with self.session() as session:
            version = session.scalar(
                select(EventRow.current_version).where(EventRow.event_id == event_id)
            )
            if version is None:
                raise KeyError(event_id)
            return int(version)

    def create_patch(self, patch: WikiPatch) -> None:
        with self.session() as session:
            if session.get(WikiPatchRow, patch.patch_id):
                raise ValueError(f"patch already exists: {patch.patch_id}")
            missing = self._missing_evidence(session, patch.evidence_ids)
            if missing:
                raise ValueError(f"unknown evidence IDs: {sorted(missing)}")
            values = patch.model_dump(mode="json")
            values["created_at"] = patch.created_at
            values["audit"] = patch.audit.model_dump(mode="json")
            session.add(WikiPatchRow(**values, status="pending"))

    def get_patch(self, patch_id: str) -> dict[str, Any] | None:
        with self.session() as session:
            row = session.get(WikiPatchRow, patch_id)
            return self._patch_dict(row) if row else None

    def list_patches(
        self, *, status: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        statement = select(WikiPatchRow).order_by(WikiPatchRow.created_at, WikiPatchRow.patch_id)
        if status:
            statement = statement.where(WikiPatchRow.status == status)
        if limit is not None:
            statement = statement.limit(limit)
        with self.session() as session:
            return [self._patch_dict(row) for row in session.scalars(statement)]

    def list_pending_patches(self) -> list[dict[str, Any]]:
        return self.list_patches(status="pending")

    def get_review_context(self, patch_id: str) -> dict[str, Any]:
        with self.session() as session:
            patch = session.get(WikiPatchRow, patch_id)
            if patch is None:
                raise KeyError(patch_id)
            evidence_rows = list(
                session.scalars(
                    select(EvidenceRow)
                    .where(EvidenceRow.evidence_id.in_(patch.evidence_ids))
                    .order_by(EvidenceRow.published_at, EvidenceRow.evidence_id)
                )
            )
            event = session.get(EventRow, patch.event_id) if patch.event_id else None
            reviews = list(
                session.scalars(
                    select(ReviewActionRow)
                    .where(ReviewActionRow.patch_id == patch_id)
                    .order_by(ReviewActionRow.created_at, ReviewActionRow.review_id)
                )
            )
            return {
                "patch": self._patch_dict(patch),
                "evidence": [
                    self._evidence_model(row).model_dump(mode="json") for row in evidence_rows
                ],
                "current_event": self._event_dict(event) if event else None,
                "review_history": [
                    {
                        "decision": row.decision,
                        "reviewer": row.reviewer,
                        "reason": row.reason,
                        "edited_payload": row.edited_payload,
                        "created_at": _utc(row.created_at),
                    }
                    for row in reviews
                ],
            }

    def reset_patch_for_rerun(self, patch_id: str, reviewer: str = "web") -> dict[str, Any]:
        with self.session() as session:
            patch = session.get(WikiPatchRow, patch_id)
            if patch is None:
                raise KeyError(patch_id)
            if patch.status != "pending":
                raise ValueError(f"only a pending patch can be rerun: {patch.status}")
            patch.status = "rerun_requested"
            session.add(
                ReviewActionRow(
                    patch_id=patch_id,
                    decision="rerun",
                    reviewer=reviewer,
                    reason="returned to the agent workflow",
                    edited_payload=None,
                    created_at=datetime.now(UTC),
                )
            )
            session.flush()
            return self._patch_dict(patch)

    def review_patch(
        self,
        patch_id: str,
        decision: str,
        edited_payload: dict[str, Any] | None = None,
        reviewer: str = "web",
        reason: str = "",
    ) -> dict[str, Any]:
        normalized = decision.lower()
        if normalized not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        validation_error: str | None = None
        with self.session() as session:
            row = session.get(WikiPatchRow, patch_id)
            if row is None:
                raise KeyError(patch_id)
            if row.status != "pending":
                raise ValueError(f"patch is not pending: {row.status}")
            if edited_payload is not None:
                row.payload = _json(edited_payload)
            if normalized == "approve":
                if not self._operation_supported(row.operation):
                    validation_error = f"operation {row.operation} is not supported for commit"
                else:
                    parsed, audit = self._validate_payload(session, row)
                    row.audit = audit.model_dump(mode="json")
                    if parsed is not None:
                        row.payload = parsed.model_dump(
                            mode="json", exclude={"schema_version"}, exclude_none=True
                        )
                    if audit.status == AuditStatus.BLOCK:
                        validation_error = "patch validation failed with BLOCK audit"
                if validation_error is None:
                    row.status = "approved"
            else:
                row.status = "rejected"
            session.add(
                ReviewActionRow(
                    patch_id=patch_id,
                    decision="blocked" if validation_error else normalized,
                    reviewer=reviewer,
                    reason=reason,
                    edited_payload=_json(edited_payload),
                    created_at=datetime.now(UTC),
                )
            )
            session.flush()
            result = self._patch_dict(row)
        if validation_error is not None:
            raise ValueError(validation_error)
        return result

    def commit_approved_patch(self, patch_id: str) -> int:
        with self.session() as session:
            patch = session.scalar(
                select(WikiPatchRow).where(WikiPatchRow.patch_id == patch_id).with_for_update()
            )
            if patch is None:
                raise KeyError(patch_id)
            if patch.status == "committed":
                version = session.scalar(
                    select(WikiVersionRow).where(WikiVersionRow.patch_id == patch_id)
                )
                assert version is not None
                return version.version
            if not self._operation_supported(patch.operation):
                raise ValueError(f"operation {patch.operation} is not supported for commit")
            if patch.status != "approved":
                raise ValueError(f"patch is not approved: {patch.status}")
            parsed, audit = self._validate_payload(session, patch)
            if parsed is None or audit.status == AuditStatus.BLOCK:
                raise ValueError("patch validation failed with BLOCK audit")
            patch.audit = audit.model_dump(mode="json")
            patch.payload = parsed.model_dump(
                mode="json", exclude={"schema_version"}, exclude_none=True
            )
            missing = self._missing_evidence(session, patch.evidence_ids)
            if missing:
                raise ValueError(f"unknown evidence IDs: {sorted(missing)}")
            event_data = patch.payload.get("event", {})
            event_id = patch.event_id or event_data.get("event_id")
            if not event_id:
                raise ValueError("patch requires an event_id")
            event = session.scalar(
                select(EventRow).where(EventRow.event_id == event_id).with_for_update()
            )
            if event is None:
                if patch.base_version != 0:
                    raise VersionConflict(
                        f"event {event_id} does not exist at version {patch.base_version}"
                    )
                event = self._new_event(event_id, event_data, patch)
                session.add(event)
                session.flush()
            elif event.current_version != patch.base_version:
                raise VersionConflict(
                    f"event {event_id} is at version {event.current_version}, "
                    f"expected {patch.base_version}"
                )
            version = patch.base_version + 1
            snapshot = self._merge_snapshot(event.snapshot or {}, patch.payload)
            self._apply_event_metadata(event, event_data)
            self._insert_claims(session, event_id, patch.payload.get("claims", []))
            self._insert_relations(session, event_id, patch.payload.get("relations", []))
            self._insert_edges(session, event_id, version, patch.payload.get("edges", []))
            event.current_version = version
            event.snapshot = snapshot
            known_at = _utc(event_data.get("known_at")) or event.known_at
            session.add(
                WikiVersionRow(
                    event_id=event_id,
                    version=version,
                    patch_id=patch_id,
                    known_at=known_at,
                    snapshot=snapshot,
                    created_at=datetime.now(UTC),
                )
            )
            patch.status = "committed"
            return version

    @staticmethod
    def _operation_supported(operation: str) -> bool:
        return operation in {
            WikiOperation.CREATE_EVENT,
            WikiOperation.UPDATE_METADATA,
            WikiOperation.APPEND_CLAIM,
            WikiOperation.ADD_RELATION,
        }

    def _validate_payload(
        self, session: Session, patch: WikiPatchRow
    ) -> tuple[PatchPayload | None, AuditResult]:
        issues: list[AuditIssue] = []
        try:
            payload = PatchPayload.model_validate(patch.payload)
        except PydanticValidationError as exc:
            issues.append(
                AuditIssue(
                    code="invalid_patch_payload",
                    message=str(exc),
                    severity="block",
                    field_ref="payload",
                )
            )
            return None, AuditResult(status=AuditStatus.BLOCK, issues=issues)

        allowed = set(patch.evidence_ids)
        rows = list(
            session.scalars(select(EvidenceRow).where(EvidenceRow.evidence_id.in_(allowed)))
        )
        evidence = {row.evidence_id: self._evidence_model(row) for row in rows}
        for evidence_id in sorted(allowed - evidence.keys()):
            issues.append(
                AuditIssue(
                    code="missing_evidence",
                    message="Patch evidence does not exist",
                    severity="block",
                    field_ref="evidence_ids",
                    evidence_id=evidence_id,
                )
            )

        references = set(payload.event.evidence_ids)
        for item in (*payload.claims, *payload.relations, *payload.edges):
            references.update(item.evidence_ids)
        for evidence_id in sorted(references - allowed):
            issues.append(
                AuditIssue(
                    code="foreign_evidence",
                    message="Payload evidence is outside the patch evidence set",
                    severity="block",
                    field_ref="payload.evidence_ids",
                    evidence_id=evidence_id,
                )
            )

        cutoff = payload.event.known_at
        issues.extend(audit_knowledge(payload.claims, payload.relations, evidence, cutoff).issues)
        for evidence_id, document in evidence.items():
            if evidence_id in references and document.known_at > cutoff:
                issues.append(
                    AuditIssue(
                        code="future_evidence",
                        message="Evidence was not known at the event cutoff",
                        severity="block",
                        field_ref="payload.evidence_ids",
                        evidence_id=evidence_id,
                    )
                )
        for index, edge in enumerate(payload.edges):
            if edge.known_at > cutoff:
                issues.append(
                    AuditIssue(
                        code="future_information",
                        message="Knowledge edge was not known at the event cutoff",
                        severity="block",
                        field_ref=f"edges.{index}.known_at",
                    )
                )
            event_node_ids = {
                node_id
                for kind, node_id in (
                    (edge.source_node_kind, edge.source_node_id),
                    (edge.target_node_kind, edge.target_node_id),
                )
                if kind == "event"
            }
            expected = patch.event_id or payload.event.event_id
            if expected and event_node_ids - {expected}:
                issues.append(
                    AuditIssue(
                        code="foreign_event_edge",
                        message="Knowledge edge references another event",
                        severity="block",
                        field_ref=f"edges.{index}",
                    )
                )

        existing_issues = [
            AuditIssue.model_validate(item) for item in (patch.audit or {}).get("issues", [])
        ]
        if (patch.audit or {}).get("status") == AuditStatus.BLOCK and not existing_issues:
            existing_issues.append(
                AuditIssue(
                    code="prior_block",
                    message="The patch was blocked by its producing agent",
                    severity="block",
                    field_ref="audit",
                )
            )
        issues = [*existing_issues, *issues]
        status = (
            AuditStatus.BLOCK
            if any(item.severity == "block" for item in issues)
            else (AuditStatus.WARN if issues else AuditStatus.PASS)
        )
        return payload, AuditResult(status=status, issues=issues)

    def list_approved_versions(self, cutoff: datetime | None = None) -> list[dict[str, Any]]:
        visible = select(
            WikiVersionRow.event_id,
            func.max(WikiVersionRow.version).label("latest_version"),
        )
        if cutoff is not None:
            visible = visible.where(WikiVersionRow.known_at <= cutoff)
        visible = visible.group_by(WikiVersionRow.event_id).subquery()
        statement = (
            select(WikiVersionRow)
            .join(
                visible,
                (WikiVersionRow.event_id == visible.c.event_id)
                & (WikiVersionRow.version == visible.c.latest_version),
            )
            .order_by(WikiVersionRow.event_id)
        )
        with self.session() as session:
            output: list[dict[str, Any]] = []
            for row in session.scalars(statement):
                patch = session.get(WikiPatchRow, row.patch_id)
                evidence_rows = (
                    list(
                        session.scalars(
                            select(EvidenceRow)
                            .where(EvidenceRow.evidence_id.in_(patch.evidence_ids))
                            .order_by(EvidenceRow.published_at, EvidenceRow.evidence_id)
                        )
                    )
                    if patch
                    else []
                )
                reviews = list(
                    session.scalars(
                        select(ReviewActionRow)
                        .where(ReviewActionRow.patch_id == row.patch_id)
                        .order_by(ReviewActionRow.created_at, ReviewActionRow.review_id)
                    )
                )
                output.append(
                    {
                        "event_id": row.event_id,
                        "version": row.version,
                        "patch_id": row.patch_id,
                        "known_at": _utc(row.known_at),
                        "snapshot": row.snapshot,
                        "created_at": _utc(row.created_at),
                        "evidence": [
                            self._evidence_model(item).model_dump(mode="json")
                            for item in evidence_rows
                        ],
                        "review_history": [
                            {
                                "decision": item.decision,
                                "reviewer": item.reviewer,
                                "reason": item.reason,
                                "edited_payload": item.edited_payload,
                                "created_at": _utc(item.created_at),
                            }
                            for item in reviews
                        ],
                        "audit": patch.audit if patch else {"status": "BLOCK"},
                        "model_version": patch.model_version if patch else None,
                        "prompt_version": patch.prompt_version if patch else None,
                        "schema_version": patch.schema_version if patch else None,
                    }
                )
            return output

    def graph_snapshot(self, cutoff: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
        cutoff = cutoff or datetime.now(UTC)
        visible = (
            select(
                WikiVersionRow.event_id,
                func.max(WikiVersionRow.version).label("latest_version"),
            )
            .where(WikiVersionRow.known_at <= cutoff)
            .group_by(WikiVersionRow.event_id)
            .subquery()
        )
        with self.session() as session:
            versions = list(
                session.scalars(
                    select(WikiVersionRow).join(
                        visible,
                        (WikiVersionRow.event_id == visible.c.event_id)
                        & (WikiVersionRow.version == visible.c.latest_version),
                    )
                )
            )
            edge_ids = {
                item.get("edge_id")
                for version in versions
                for item in version.snapshot.get("edges", [])
                if item.get("edge_id")
            }
            edges = (
                list(
                    session.scalars(select(EventEdgeRow).where(EventEdgeRow.edge_id.in_(edge_ids)))
                )
                if edge_ids
                else []
            )
        nodes: dict[tuple[str, str], dict[str, Any]] = {}
        visible_events: set[str] = set()
        for version in versions:
            event = version.snapshot.get("event", {})
            if event.get("status") == "archived":
                continue
            visible_events.add(version.event_id)
            nodes[("event", version.event_id)] = {
                "node_kind": "event",
                "node_id": version.event_id,
                "label": event.get("event_title", version.event_id),
                "known_at": _utc(version.known_at),
            }
        output_edges: list[dict[str, Any]] = []
        for edge in edges:
            if edge.event_id not in visible_events:
                continue
            for kind, node_id in (
                (edge.source_node_kind, edge.source_node_id),
                (edge.target_node_kind, edge.target_node_id),
            ):
                nodes.setdefault(
                    (kind, node_id), {"node_kind": kind, "node_id": node_id, "label": node_id}
                )
            output_edges.append(
                {
                    "edge_id": edge.edge_id,
                    "event_id": edge.event_id,
                    "source_node_kind": edge.source_node_kind,
                    "source_node_id": edge.source_node_id,
                    "target_node_kind": edge.target_node_kind,
                    "target_node_id": edge.target_node_id,
                    "relation_type": edge.relation_type,
                    "known_at": _utc(edge.known_at),
                    "valid_from": _utc(edge.valid_from),
                    "evidence_ids": edge.evidence_ids,
                    "version": edge.version,
                }
            )
        return {
            "nodes": sorted(nodes.values(), key=lambda item: (item["node_kind"], item["node_id"])),
            "edges": sorted(output_edges, key=lambda item: item["edge_id"]),
        }

    def status_counts(self) -> dict[str, int]:
        with self.session() as session:
            counts = {
                "evidence_documents": session.scalar(select(func.count()).select_from(EvidenceRow))
                or 0,
                "candidate_bundles": session.scalar(select(func.count()).select_from(CandidateRow))
                or 0,
                "events": session.scalar(select(func.count()).select_from(EventRow)) or 0,
                "wiki_versions": session.scalar(select(func.count()).select_from(WikiVersionRow))
                or 0,
            }
            for status, count in session.execute(
                select(WikiPatchRow.status, func.count()).group_by(WikiPatchRow.status)
            ):
                counts[f"patches_{status}"] = count
            return counts

    def audit_metrics(self) -> dict[str, Any]:
        with self.session() as session:
            total = session.scalar(select(func.count()).select_from(WikiPatchRow)) or 0
            statuses = dict(
                session.execute(
                    select(WikiPatchRow.status, func.count()).group_by(WikiPatchRow.status)
                ).all()
            )
            audits = [row or {} for row in session.scalars(select(WikiPatchRow.audit))]
        audit_statuses: dict[str, int] = {}
        block_reasons: dict[str, int] = {}
        for audit in audits:
            status = str(audit.get("status", "UNKNOWN"))
            audit_statuses[status] = audit_statuses.get(status, 0) + 1
            for issue in audit.get("issues", []):
                if issue.get("severity") == "block":
                    code = str(issue.get("code", "unknown"))
                    block_reasons[code] = block_reasons.get(code, 0) + 1
        return {
            "total_patches": total,
            "patch_statuses": statuses,
            "audit_statuses": audit_statuses,
            "block_reasons": block_reasons,
        }

    @staticmethod
    def _missing_evidence(session: Session, evidence_ids: Sequence[str]) -> set[str]:
        requested = set(evidence_ids)
        found = set(
            session.scalars(
                select(EvidenceRow.evidence_id).where(EvidenceRow.evidence_id.in_(requested))
            )
        )
        return requested - found

    @staticmethod
    def _new_event(event_id: str, data: dict[str, Any], patch: WikiPatchRow) -> EventRow:
        required = {"event_family", "event_subject", "event_title", "event_time", "known_at"}
        missing = required - data.keys()
        if missing:
            raise ValueError(f"create_event payload missing fields: {sorted(missing)}")
        return EventRow(
            event_id=event_id,
            event_family=str(data["event_family"]),
            event_subject=data["event_subject"],
            event_title=data["event_title"],
            event_time=_utc(data["event_time"]),
            known_at=_utc(data["known_at"]),
            current_version=0,
            snapshot={},
        )

    @staticmethod
    def _apply_event_metadata(event: EventRow, data: dict[str, Any]) -> None:
        for key in ("event_family", "event_subject", "event_title", "status"):
            if key in data:
                setattr(event, key, str(data[key]))
        for key in ("event_time", "known_at"):
            if key in data:
                setattr(event, key, _utc(data[key]))

    def _insert_claims(self, session: Session, event_id: str, claims: list[dict[str, Any]]) -> None:
        for data in claims:
            missing = self._missing_evidence(session, data.get("evidence_ids", []))
            if missing:
                raise ValueError(f"unknown claim evidence IDs: {sorted(missing)}")
            values = dict(data)
            values.pop("schema_version", None)
            values["event_id"] = event_id
            values["event_time"] = _utc(values["event_time"])
            values["known_at"] = _utc(values["known_at"])
            session.add(ClaimRow(**values))

    def _insert_relations(
        self, session: Session, event_id: str, relations: list[dict[str, Any]]
    ) -> None:
        for data in relations:
            missing = self._missing_evidence(session, data.get("evidence_ids", []))
            if missing:
                raise ValueError(f"unknown relation evidence IDs: {sorted(missing)}")
            values = dict(data)
            values.pop("schema_version", None)
            values["event_id"] = event_id
            values["known_at"] = _utc(values["known_at"])
            values["valid_from"] = _utc(values.get("valid_from"))
            session.add(RelationRow(**values))

    def _insert_edges(
        self,
        session: Session,
        event_id: str,
        version: int,
        edges: list[dict[str, Any]],
    ) -> None:
        for data in edges:
            missing = self._missing_evidence(session, data.get("evidence_ids", []))
            if missing:
                raise ValueError(f"unknown edge evidence IDs: {sorted(missing)}")
            values = dict(data)
            values.pop("schema_version", None)
            values.update(event_id=event_id, version=version)
            values["known_at"] = _utc(values["known_at"])
            values["valid_from"] = _utc(values.get("valid_from"))
            session.add(EventEdgeRow(**values))

    @staticmethod
    def _merge_snapshot(previous: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(previous)
        for key, value in _json(payload).items():
            if key in {"claims", "relations", "edges"} and isinstance(value, list):
                old = result.get(key, [])
                identity = {"claims": "claim_id", "relations": "relation_id", "edges": "edge_id"}[
                    key
                ]
                merged = {item.get(identity): item for item in old}
                merged.update({item.get(identity): item for item in value})
                result[key] = list(merged.values())
            elif key == "event" and isinstance(value, dict):
                result[key] = {**result.get(key, {}), **value}
            else:
                result[key] = value
        return result

    @staticmethod
    def _event_dict(row: EventRow) -> dict[str, Any]:
        return {
            "event_id": row.event_id,
            "event_family": row.event_family,
            "event_subject": row.event_subject,
            "event_title": row.event_title,
            "event_time": _utc(row.event_time),
            "known_at": _utc(row.known_at),
            "current_version": row.current_version,
            "status": row.status,
            "snapshot": row.snapshot,
        }

    @staticmethod
    def _patch_dict(row: WikiPatchRow) -> dict[str, Any]:
        return {
            "patch_id": row.patch_id,
            "thread_id": row.thread_id,
            "operation": row.operation,
            "event_id": row.event_id,
            "base_version": row.base_version,
            "evidence_ids": row.evidence_ids,
            "payload": row.payload,
            "audit": row.audit,
            "model_version": row.model_version,
            "prompt_version": row.prompt_version,
            "created_at": _utc(row.created_at),
            "status": row.status,
        }
