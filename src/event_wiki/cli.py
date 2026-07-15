from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer

from event_wiki.config import settings
from event_wiki.ingestion import DateWindow, latest_complete_month
from event_wiki.security import scan_public_tree

app = typer.Typer(no_args_is_help=True, help="AIME evidence-first Event Wiki")
export_app = typer.Typer(no_args_is_help=True, help="Export approved Wiki knowledge")
maintenance_app = typer.Typer(no_args_is_help=True, help="本地维护与确定性修复")
app.add_typer(export_app, name="export")
app.add_typer(maintenance_app, name="maintenance")


def _repository():
    from event_wiki.db import Repository

    return Repository.from_url(settings.database_url)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _window(month: str | None, start: str | None, end: str | None) -> DateWindow:
    if month:
        year, month_number = map(int, month.split("-"))
        begin = datetime(year, month_number, 1, tzinfo=UTC)
        finish = datetime(
            year + (month_number == 12),
            1 if month_number == 12 else month_number + 1,
            1,
            tzinfo=UTC,
        )
        return DateWindow(begin, finish)
    if start and end:
        return DateWindow(_timestamp(start), _timestamp(end))
    if start or end:
        raise typer.BadParameter("--start and --end must be provided together")
    sources = [
        settings.data_root / "v1" / name
        for name in ("US_NEWS.jsonl", "US_FLASH.jsonl", "US_NOTICE.jsonl")
    ]
    return latest_complete_month(sources)


@app.command("init-db")
def init_db() -> None:
    """Create the local database schema (Alembic is preferred for shared deployments)."""
    from event_wiki.db import Repository

    Repository.from_url(settings.database_url, create_schema=True).close()
    typer.echo("database schema ready")


@app.command()
def ingest(
    month: str | None = typer.Option(None),
    start: str | None = typer.Option(None),
    end: str | None = typer.Option(None),
    skip_input_hash: bool = typer.Option(False),
) -> None:
    """Stream a fixed time window from the read-only cleaned corpus."""
    from event_wiki.pipeline import ingest_sources

    manifest = ingest_sources(
        _repository(),
        settings.data_root,
        _window(month, start, end),
        settings.local_output_dir / "manifests",
        hash_inputs=not skip_input_hash,
    )
    typer.echo(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False))


@app.command()
def candidates(
    limit: int | None = typer.Option(None),
    rebuild_pending: bool = typer.Option(
        False, "--rebuild-pending", help="删除未处理候选并按当前召回规则重建"
    ),
) -> None:
    """Build deterministic candidate bundles from ingested evidence."""
    from event_wiki.pipeline import generate_candidates

    repository = _repository()
    deleted = repository.delete_candidates(status="pending") if rebuild_pending else 0
    created = generate_candidates(repository, limit=limit)
    typer.echo(f"deleted {deleted} pending candidates; created {created} candidates")


@app.command()
def run(limit: int | None = typer.Option(None), offline: bool = typer.Option(False)) -> None:
    """Run pending candidates through the LangGraph workflow."""
    from event_wiki.graph import GraphRunner

    count = GraphRunner.from_settings(_repository(), offline=offline).run_pending(limit=limit)
    typer.echo(f"started {count} candidate workflows")


@app.command()
def retry(run_id: str) -> None:
    from event_wiki.graph import GraphRunner

    GraphRunner.from_settings(_repository()).retry(run_id)
    typer.echo(f"retried {run_id}")


@app.command()
def resume(
    patch_id: str, decision: str = typer.Option(..., help="approve, reject, or rerun")
) -> None:
    from event_wiki.graph import GraphRunner

    GraphRunner.from_settings(_repository()).resume_patch(patch_id, decision)
    typer.echo(f"resumed {patch_id} with {decision}")


@app.command()
def status() -> None:
    typer.echo(json.dumps(_repository().status_counts(), ensure_ascii=False, indent=2))


@app.command("link")
def link_events(
    event_id: str | None = typer.Option(None, "--event-id"),
    all_events: bool = typer.Option(False, "--all"),
    limit: int = typer.Option(20, min=1),
    offline: bool = typer.Option(False),
) -> None:
    """为已提交事件生成跨事件链接。"""
    if bool(event_id) == all_events:
        raise typer.BadParameter("必须且只能指定 --event-id 或 --all")
    from event_wiki.link_graph import EventLinkRunner

    repository = _repository()
    runner = EventLinkRunner.from_settings(repository, offline=offline)
    event_ids = (
        [event_id]
        if event_id
        else [item["event_id"] for item in repository.list_events(limit=limit)]
    )
    results = [runner.run(value) for value in event_ids]
    typer.echo(json.dumps(results, ensure_ascii=False, default=str, indent=2))


@maintenance_app.command("repair-event-ids")
def repair_event_ids(
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="默认只预览修复"),
) -> None:
    """确定性修复尚未提交的创建事件 Patch ID 引用。"""
    repository = _repository()
    try:
        report = repository.repair_pending_event_ids(apply=not dry_run)
    finally:
        repository.close()
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    offline: bool = typer.Option(False, help="Use heuristic agents and an in-memory checkpoint"),
    review_token: str | None = typer.Option(
        None, help="Bearer/X-Review-Token required for non-loopback access"
    ),
) -> None:
    """Start the human review application."""
    import uvicorn

    from event_wiki.graph import GraphRunner
    from event_wiki.review import create_app

    token = review_token or settings.review_token or None
    if host not in {"127.0.0.1", "::1", "localhost"} and not token:
        raise typer.BadParameter("--review-token is required when serving beyond loopback")
    repository = _repository()
    runner = GraphRunner.from_settings(repository, offline=offline)
    uvicorn.run(
        create_app(repository, resume_callback=runner.resume_patch, review_token=token),
        host=host,
        port=port,
    )


@export_app.command("markdown")
def export_markdown(output: Path = settings.local_output_dir / "wiki") -> None:
    from event_wiki.export import export_markdown_pages

    typer.echo(f"wrote {export_markdown_pages(_repository(), output)} pages")


@export_app.command("structured")
def export_structured(
    cutoff: str,
    output: Path = settings.local_output_dir / "structured_event.jsonl",
) -> None:
    from event_wiki.export import export_structured_jsonl

    typer.echo(f"wrote {export_structured_jsonl(_repository(), output, _timestamp(cutoff))} events")


@export_app.command("audit-report")
def export_audit_report(output: Path = settings.local_output_dir / "audit_report.json") -> None:
    from event_wiki.export import export_audit_report as write_report

    write_report(_repository(), output)
    typer.echo(str(output))


@export_app.command("graph")
def export_graph(output: Path = settings.local_output_dir / "knowledge_graph.json") -> None:
    from event_wiki.export import export_graph_json

    export_graph_json(_repository(), output)
    typer.echo(str(output))


@app.command("security-scan")
def security_scan(root: Path = Path(".")) -> None:
    violations = scan_public_tree(root.resolve())
    for violation in violations:
        typer.echo(f"{violation.code}: {violation.path}: {violation.message}", err=True)
    if violations:
        raise typer.Exit(1)
    typer.echo("public repository safety scan passed")


if __name__ == "__main__":
    app()
