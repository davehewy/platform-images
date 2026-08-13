from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from platform_images.errors import ProcessError


@dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    returncode: int


class ProcessRunner:
    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture_output: bool = False,
        input_text: str | None = None,
    ) -> ProcessResult:
        try:
            completed = subprocess.run(
                list(arguments),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                check=False,
                text=True,
                capture_output=capture_output,
                input=input_text,
            )
        except OSError as exc:
            raise ProcessError(f"unable to start command: {shlex.join(arguments)}: {exc}") from exc
        result = ProcessResult(completed.stdout or "", completed.stderr or "", completed.returncode)
        if completed.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            suffix = f"\n{detail}" if detail else ""
            raise ProcessError(
                f"command failed ({completed.returncode}): {shlex.join(arguments)}{suffix}"
            )
        return result
