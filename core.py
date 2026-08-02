from __future__ import annotations

import fnmatch
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


class PolicyError(RuntimeError):
    pass


@dataclass
class AuditEvent:
    tool: str
    allowed: bool
    detail: str
    timestamp: float = field(default_factory=time.time)


class RepositoryTools:
    """MCP-style repository tools with path and mutation policies."""

    def __init__(self, root: Path, allowed_commands: Sequence[Sequence[str]] = ()) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"repository root does not exist: {self.root}")
        self.allowed_commands = {tuple(command) for command in allowed_commands}
        self.audit: list[AuditEvent] = []

    def _record(self, tool: str, allowed: bool, detail: str) -> None:
        self.audit.append(AuditEvent(tool, allowed, detail))

    def _resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            self._record("resolve_path", False, relative_path)
            raise PolicyError(f"path escapes repository root: {relative_path}")
        return candidate

    def list_files(self, pattern: str = "*") -> list[str]:
        files = sorted(str(path.relative_to(self.root)).replace("\\", "/") for path in self.root.rglob("*") if path.is_file())
        result = [path for path in files if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern)]
        self._record("list_files", True, f"pattern={pattern}; count={len(result)}")
        return result

    def read_file(self, relative_path: str) -> str:
        path = self._resolve(relative_path)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._record("read_file", False, f"{relative_path}: {exc}")
            raise
        self._record("read_file", True, relative_path)
        return content

    def search_text(self, query: str) -> list[dict[str, object]]:
        matches: list[dict[str, object]] = []
        for relative in self.list_files("*"):
            try:
                for number, line in enumerate(self.read_file(relative).splitlines(), 1):
                    if query.lower() in line.lower():
                        matches.append({"path": relative, "line": number, "text": line.strip()})
            except (OSError, UnicodeError):
                continue
        self._record("search_text", True, f"query={query}; count={len(matches)}")
        return matches

    def write_file(self, relative_path: str, content: str, approved: bool) -> None:
        if not approved:
            self._record("write_file", False, f"approval required: {relative_path}")
            raise PolicyError("mutation requires explicit approval")
        path = self._resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._record("write_file", True, relative_path)

    def run_test(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        normalized = tuple(command)
        if not self.allowed_commands:
            # No verification commands configured: skip the test (same effect as a
            # plan without test_command), but keep an audit trail of the skip.
            self._record("run_test", False, "skipped: empty allow-list (no verification configured)")
            return subprocess.CompletedProcess(list(command), 0, "", "")
        if normalized not in self.allowed_commands:
            self._record("run_test", False, " ".join(command))
            raise PolicyError("command is not in the test allow-list")
        result = subprocess.run(command, cwd=self.root, text=True, capture_output=True, timeout=30, shell=False)
        self._record("run_test", result.returncode == 0, f"exit={result.returncode}; command={' '.join(command)}")
        return result

    def tool_schemas(self) -> list[dict[str, object]]:
        return [
            {"name": "list_files", "mutating": False, "input": {"pattern": "string"}},
            {"name": "read_file", "mutating": False, "input": {"relative_path": "string"}},
            {"name": "search_text", "mutating": False, "input": {"query": "string"}},
            {"name": "write_file", "mutating": True, "input": {"relative_path": "string", "content": "string"}},
            {"name": "run_test", "mutating": False, "input": {"command": "string[]"}},
        ]


@dataclass(frozen=True)
class WriteRequest:
    path: str
    content: str


@dataclass(frozen=True)
class ChangePlan:
    summary: str
    writes: tuple[WriteRequest, ...]
    test_command: tuple[str, ...] | None = None


@dataclass
class RunReport:
    status: str
    plan: ChangePlan
    changed_files: list[str]
    test_exit_code: int | None
    trace: list[dict[str, object]]
    audit: list[AuditEvent]


