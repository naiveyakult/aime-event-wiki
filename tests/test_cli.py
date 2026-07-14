from click import unstyle
from typer.testing import CliRunner

from event_wiki.cli import app, settings


def test_cli_help_lists_core_workflows() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "ingest",
        "candidates",
        "run",
        "resume",
        "status",
        "serve",
        "export",
        "maintenance",
    ):
        assert command in result.stdout


def test_init_db_command_uses_repository_factory(tmp_path) -> None:
    original = settings.database_url
    settings.database_url = f"sqlite+pysqlite:///{tmp_path / 'cli.db'}"
    try:
        result = CliRunner().invoke(app, ["init-db"])
    finally:
        settings.database_url = original
    assert result.exit_code == 0
    assert "database schema ready" in result.stdout


def test_serve_requires_token_for_non_loopback_host() -> None:
    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code != 0
    assert "review-token is required" in unstyle(result.output)


def test_repair_event_ids_command_defaults_to_dry_run(tmp_path) -> None:
    original = settings.database_url
    settings.database_url = f"sqlite+pysqlite:///{tmp_path / 'maintenance.db'}"
    try:
        CliRunner().invoke(app, ["init-db"])
        result = CliRunner().invoke(app, ["maintenance", "repair-event-ids"])
    finally:
        settings.database_url = original

    assert result.exit_code == 0
    assert '"dry_run": true' in result.stdout
