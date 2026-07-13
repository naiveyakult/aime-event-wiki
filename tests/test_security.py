from pathlib import Path

from event_wiki.security import scan_public_tree


def test_security_scan_detects_private_artifact_and_secret(tmp_path: Path) -> None:
    (tmp_path / "private.jsonl").write_text('{"private": true}\n')
    (tmp_path / "code.py").write_text("OPENAI_API_KEY='sk-" + "x" * 30 + "'\n")
    violations = scan_public_tree(tmp_path)
    codes = {violation.code for violation in violations}
    assert "private_extension" in codes
    assert "secret_pattern" in codes


def test_security_scan_allows_synthetic_fixture_extension(tmp_path: Path) -> None:
    fixture = tmp_path / "tests" / "fixtures"
    fixture.mkdir(parents=True)
    (fixture / "synthetic.json").write_text('{"company": "Example Corp"}')
    assert scan_public_tree(tmp_path) == []


def test_security_scan_detects_provider_tokens_and_unquoted_env_values(tmp_path: Path) -> None:
    (tmp_path / "github.txt").write_text("github_pat_" + "A" * 60)
    (tmp_path / "aws.txt").write_text("AKIA" + "A" * 16)
    (tmp_path / "config.env.sample").write_text("SERVICE_TOKEN=" + "t" * 32)
    violations = scan_public_tree(tmp_path)
    assert len([item for item in violations if item.code == "secret_pattern"]) == 3
