from pathlib import Path

from event_wiki.config import Settings

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", ".venv", ".pytest_cache", ".ruff_cache", ".local"}


def test_repository_markdown_documents_are_written_in_chinese() -> None:
    markdown_files = [
        path
        for path in ROOT.rglob("*.md")
        if not EXCLUDED_PARTS.intersection(path.relative_to(ROOT).parts)
    ]

    assert markdown_files
    missing_chinese = [
        str(path.relative_to(ROOT))
        for path in markdown_files
        if not any("\u4e00" <= character <= "\u9fff" for character in path.read_text())
    ]
    assert missing_chinese == []


def test_default_model_matches_project_configuration() -> None:
    assert Settings(_env_file=None).openai_model == "deepseek-v4-pro"
    assert "OPENAI_MODEL=deepseek-v4-pro" in (ROOT / ".env.example").read_text()
