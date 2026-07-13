from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PRIVATE_EXTENSIONS = {".jsonl", ".ndjson", ".parquet", ".duckdb", ".db", ".sqlite"}
SKIP_PARTS = {".git", ".venv", ".local", "__pycache__", ".pytest_cache", ".ruff_cache"}
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"gh[opusr]_[A-Za-z0-9]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(
        r"(?im)^[ \t]*(?:export[ \t]+)?[A-Z][A-Z0-9_]*(?:API_KEY|SECRET|TOKEN)"
        r"[ \t]*=[ \t]*(?:['\"][^'\"\r\n]{12,}['\"]|[A-Za-z0-9_./+:-]{12,})"
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


@dataclass(frozen=True)
class SecurityViolation:
    code: str
    path: Path
    message: str


def scan_public_tree(root: Path, *, max_bytes: int = 5 * 1024 * 1024) -> list[SecurityViolation]:
    violations: list[SecurityViolation] = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.relative_to(root).parts):
            continue
        relative = path.relative_to(root)
        if path.suffix.lower() in PRIVATE_EXTENSIONS:
            violations.append(
                SecurityViolation(
                    "private_extension",
                    relative,
                    "private/generated data extension is not publishable",
                )
            )
            continue
        if path.stat().st_size > max_bytes:
            violations.append(
                SecurityViolation(
                    "large_file", relative, "file exceeds public repository size policy"
                )
            )
            continue
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            continue
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            violations.append(
                SecurityViolation("secret_pattern", relative, "possible credential or private key")
            )
    return violations
