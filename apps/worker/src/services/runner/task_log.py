"""The per-task log file the dashboard's Task Log panel tails.

Every runner phase appends human-readable progress here. Writes are
synchronous and flushed immediately: the file is small, the panel polls it,
and simplicity beats throughput for this log.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

_RULE = "=" * 60


class TaskLog:
    """Append-only text log at ``<log_dir>/<task_key>.log``.

    Also usable wherever a writable text file is expected (the verifier
    streams command output into it).
    """

    def __init__(self, log_dir: str, task_key: str) -> None:
        self.path = Path(log_dir) / f"{task_key}.log"
        self._file = self.path.open("a", encoding="utf-8")

    def write(self, text: str) -> None:
        self._file.write(text)
        self._file.flush()

    def flush(self) -> None:
        self._file.flush()

    def note(self, tag: str, message: str) -> None:
        """Write one ``[tag] message`` line, the log's standard entry format."""
        self.write(f"\n[{tag}] {message}\n")

    def banner(self, message: str) -> None:
        self.write(f"\n{_RULE}\n{message}\n{_RULE}\n")

    def started(self, task_key: str) -> None:
        self.banner(f"Task {task_key} started at {datetime.now(UTC).isoformat()}")

    def finished(self, task_key: str, *, success: bool) -> None:
        self.banner(
            f"Task {task_key} finished at {datetime.now(UTC).isoformat()} (success={success})"
        )

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
