from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "_mapping"):
        return dict(value._mapping)
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {key: item for key, item in value.__dict__.items() if not key.startswith("_")}
    return value


def _dict(value: Any) -> dict[str, Any]:
    converted = _jsonable(value)
    return dict(converted) if isinstance(converted, dict) else {}


def _parse_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _visible(value: dict[str, Any], cutoff: datetime | None) -> bool:
    known_at = _parse_time(value.get("known_at"))
    if known_at is None:
        return False
    return cutoff is None or known_at <= cutoff


def _approved_events(repository: Any, cutoff: datetime | None = None) -> list[dict[str, Any]]:
    if hasattr(repository, "list_approved_versions"):
        versions = repository.list_approved_versions(cutoff=cutoff)
        events: list[dict[str, Any]] = []
        for value in versions:
            version = _dict(value)
            snapshot = _dict(version.get("snapshot"))
            metadata = _dict(snapshot.get("event"))
            event = {key: item for key, item in snapshot.items() if key != "event"}
            event.update(metadata)
            for key in (
                "event_id",
                "version",
                "patch_id",
                "known_at",
                "created_at",
                "evidence",
                "review_history",
                "audit",
                "model_version",
                "prompt_version",
                "schema_version",
            ):
                if key in version and key not in event:
                    event[key] = version[key]
            event.setdefault("status", "approved")
            events.append(event)
        return events
    return [_dict(value) for value in repository.list_export_events()]


def _audit_status(event: dict[str, Any]) -> str:
    audit = _dict(event.get("audit"))
    return str(audit.get("status") or event.get("audit_status") or "PASS").upper()


def _prepare_event(event: dict[str, Any], cutoff: datetime | None) -> dict[str, Any] | None:
    if str(event.get("status", "approved")).lower() not in {"approved", "committed"}:
        return None
    if _audit_status(event) == "BLOCK" or not _visible(event, cutoff):
        return None
    prepared = dict(event)
    prepared["claims"] = [
        _dict(item) for item in event.get("claims", []) if _visible(_dict(item), cutoff)
    ]
    prepared["relations"] = [
        _dict(item) for item in event.get("relations", []) if _visible(_dict(item), cutoff)
    ]
    used_evidence = {
        evidence_id
        for collection in (prepared["claims"], prepared["relations"])
        for item in collection
        for evidence_id in item.get("evidence_ids", [])
    }
    evidence = [_dict(item) for item in event.get("evidence", [])]
    if used_evidence and evidence:
        evidence = [item for item in evidence if item.get("evidence_id") in used_evidence]
    prepared["evidence"] = evidence
    return _jsonable(prepared)


def iter_exportable_events(repository: Any, cutoff: datetime | None = None):
    for event in _approved_events(repository, cutoff):
        prepared = _prepare_event(event, cutoff)
        if prepared is not None:
            yield prepared


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=_jsonable) + "\n")


def export_structured(repository: Any, output_path: Path, *, cutoff: datetime) -> int:
    events = list(iter_exportable_events(repository, cutoff))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, default=_jsonable) + "\n")
    return len(events)


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "event"


def _markdown(event: dict[str, Any]) -> str:
    title = event.get("title") or event.get("event_title") or event.get("event_id")
    lines = [f"# {title}", ""]
    for label, key in (
        ("Event ID", "event_id"),
        ("Family", "event_family"),
        ("Subject", "event_subject"),
        ("Event time", "event_time"),
        ("Known at", "known_at"),
        ("Version", "version"),
    ):
        if event.get(key) is not None:
            lines.append(f"- **{label}:** {event[key]}")
    lines.extend(["", "## Claims", ""])
    for claim in event.get("claims", []):
        lines.append(
            f"- `{claim.get('claim_id', '')}` {claim.get('subject', '')} "
            f"**{claim.get('predicate', '')}** {claim.get('object_value', '')}"
        )
        if claim.get("evidence_ids"):
            lines.append(f"  - Evidence: {', '.join(claim['evidence_ids'])}")
    lines.extend(["", "## Relations", ""])
    for relation in event.get("relations", []):
        lines.append(
            f"- {relation.get('source_entity', '')} —**{relation.get('relation_type', '')}**→ "
            f"{relation.get('target_entity', '')}"
        )
        if relation.get("evidence_ids"):
            lines.append(f"  - Evidence: {', '.join(relation['evidence_ids'])}")
    lines.extend(["", "## Evidence", ""])
    for evidence in event.get("evidence", []):
        lines.append(f"- `{evidence.get('evidence_id', '')}` {evidence.get('title', '')}")
    return "\n".join(lines).rstrip() + "\n"


def export_markdown(
    repository: Any, output_dir: Path, *, cutoff: datetime | None = None
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for event in iter_exportable_events(repository, cutoff):
        path = output_dir / f"{_safe_name(str(event.get('event_id') or 'event'))}.md"
        path.write_text(_markdown(event), encoding="utf-8")
        paths.append(path)
    return paths


def export_markdown_pages(repository: Any, output_dir: Path, cutoff: datetime | None = None) -> int:
    """CLI-facing Markdown exporter; returns the number of generated pages."""
    return len(export_markdown(repository, output_dir, cutoff=cutoff))


def export_structured_jsonl(repository: Any, output_path: Path, cutoff: datetime) -> int:
    """CLI-facing structured exporter with a required historical cutoff."""
    return export_structured(repository, output_path, cutoff=cutoff)


def export_audit_report(repository: Any, output_path: Path) -> dict[str, Any]:
    if hasattr(repository, "audit_metrics"):
        metrics = _dict(repository.audit_metrics())
        report = {"generated_at": datetime.now(UTC), "metrics": metrics}
    else:
        events = [_dict(value) for value in repository.list_export_events()]
        blocked = sum(_audit_status(event) == "BLOCK" for event in events)
        exportable = sum(_prepare_event(event, None) is not None for event in events)
        report = {
            "generated_at": datetime.now(UTC),
            "summary": {
                "total": len(events),
                "blocked": blocked,
                "approved_exportable": exportable,
            },
            "events": [
                {
                    "event_id": event.get("event_id"),
                    "status": event.get("status"),
                    "audit": event.get("audit"),
                }
                for event in events
            ],
        }
    _write_json(output_path, report)
    return _jsonable(report)


def _graph_from_events(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    nodes: dict[tuple[str, str], dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def node(kind: str, identifier: str, **properties: Any) -> None:
        if identifier:
            nodes.setdefault((kind, identifier), {"type": kind, "id": identifier, **properties})

    def edge(source: str, target: str, kind: str, **properties: Any) -> None:
        edges.setdefault(
            (source, target, kind),
            {"source": source, "target": target, "type": kind, **properties},
        )

    for event in events:
        event_id = str(event.get("event_id") or "")
        node("event", event_id, label=event.get("title") or event.get("event_title"))
        subject = str(event.get("event_subject") or "")
        if subject:
            node("entity", subject, label=subject)
            edge(event_id, subject, "has_subject")
        for claim in event.get("claims", []):
            claim_id = str(claim.get("claim_id") or "")
            node("claim", claim_id, label=claim.get("predicate"))
            edge(event_id, claim_id, "has_claim")
            entity = str(claim.get("subject") or "")
            node("entity", entity, label=entity)
            edge(claim_id, entity, "about")
            for evidence_id in claim.get("evidence_ids", []):
                node("evidence", str(evidence_id), label=str(evidence_id))
                edge(claim_id, str(evidence_id), "supported_by")
        for relation in event.get("relations", []):
            source = str(relation.get("source_entity") or "")
            target = str(relation.get("target_entity") or "")
            node("entity", source, label=source)
            node("entity", target, label=target)
            edge(
                source,
                target,
                str(relation.get("relation_type") or "related_to"),
                event_id=event_id,
            )
            relation_id = str(relation.get("relation_id") or f"{source}:{target}")
            for evidence_id in relation.get("evidence_ids", []):
                node("evidence", str(evidence_id), label=str(evidence_id))
                edge(relation_id, str(evidence_id), "supported_by")
        for evidence in event.get("evidence", []):
            evidence_id = str(evidence.get("evidence_id") or "")
            node("evidence", evidence_id, label=evidence.get("title") or evidence_id)
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def export_graph_json(
    repository: Any, output_path: Path, *, cutoff: datetime | None = None
) -> dict[str, Any]:
    if hasattr(repository, "graph_snapshot"):
        raw = _dict(repository.graph_snapshot(cutoff=cutoff))
        nodes = []
        for item in raw.get("nodes", []):
            item = _dict(item)
            item.setdefault("type", item.get("node_kind"))
            item.setdefault("id", item.get("node_id"))
            nodes.append(item)
        edges = []
        for item in raw.get("edges", []):
            item = _dict(item)
            item.setdefault("source", item.get("source_node_id"))
            item.setdefault("target", item.get("target_node_id"))
            item.setdefault("type", item.get("relation_type"))
            edges.append(item)
        graph = {"nodes": nodes, "edges": edges}
    else:
        graph = _graph_from_events(list(iter_exportable_events(repository, cutoff)))
    graph["generated_at"] = datetime.now(UTC)
    graph["cutoff"] = cutoff
    _write_json(output_path, graph)
    return _jsonable(graph)
