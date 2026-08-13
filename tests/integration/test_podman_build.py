from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from platform_images.cli import main


def podman_available() -> bool:
    if shutil.which("podman") is None:
        return False
    result = subprocess.run(
        ["podman", "info"], check=False, capture_output=True, text=True, timeout=15
    )
    return result.returncode == 0


@pytest.mark.integration
@pytest.mark.skipif(not podman_available(), reason="a working Podman service is unavailable")
def test_curl_consumes_controller_selected_local_base() -> None:
    root = Path(__file__).parents[2]
    assert main(["images", "build", "curl"], cwd=root) == 0
    version = subprocess.run(
        ["podman", "run", "--rm", "localhost/platform-images/curl:dev", "--version"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    assert version.returncode == 0, version.stderr
    assert "curl" in version.stdout.lower()
    marker = subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "localhost/platform-images/curl:dev",
            "-c",
            'test "$(cat /etc/platform-base-marker)" = "platform-local-base-v1"',
        ],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    assert marker.returncode == 0, marker.stderr
