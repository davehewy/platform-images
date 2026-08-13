from __future__ import annotations

import uuid

import pytest

from platform_images.errors import ProcessError
from platform_images.process import ProcessRunner


def test_missing_executable_is_an_explicit_process_error() -> None:
    executable = f"missing-platform-images-command-{uuid.uuid4()}"
    with pytest.raises(ProcessError, match="unable to start command"):
        ProcessRunner().run([executable])
