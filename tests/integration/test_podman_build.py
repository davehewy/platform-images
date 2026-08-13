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


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"], check=False, capture_output=True, text=True, timeout=15
    )
    buildx = subprocess.run(
        ["docker", "buildx", "version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0 and buildx.returncode == 0


def buildah_available() -> bool:
    if shutil.which("buildah") is None:
        return False
    result = subprocess.run(
        ["buildah", "info"], check=False, capture_output=True, text=True, timeout=15
    )
    return result.returncode == 0


def nerdctl_available() -> bool:
    if shutil.which("nerdctl") is None or shutil.which("buildctl") is None:
        return False
    result = subprocess.run(
        ["nerdctl", "info"], check=False, capture_output=True, text=True, timeout=15
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


@pytest.mark.integration
@pytest.mark.skipif(not docker_available(), reason="a working Docker Buildx service is unavailable")
def test_docker_buildx_consumes_controller_selected_local_base() -> None:
    root = Path(__file__).parents[2]
    assert main(["images", "build", "curl", "--engine", "docker"], cwd=root) == 0
    marker = subprocess.run(
        [
            "docker",
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


@pytest.mark.integration
@pytest.mark.skipif(not buildah_available(), reason="a working Buildah setup is unavailable")
def test_buildah_consumes_controller_selected_local_base() -> None:
    root = Path(__file__).parents[2]
    assert main(["images", "build", "curl", "--builder", "buildah"], cwd=root) == 0
    created = subprocess.run(
        ["buildah", "from", "localhost/platform-images/curl:dev"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    assert created.returncode == 0, created.stderr
    container = created.stdout.strip()
    try:
        marker = subprocess.run(
            [
                "buildah",
                "run",
                container,
                "--",
                "sh",
                "-c",
                'test "$(cat /etc/platform-base-marker)" = "platform-local-base-v1"',
            ],
            cwd=root,
            check=False,
            text=True,
            capture_output=True,
        )
        assert marker.returncode == 0, marker.stderr
    finally:
        subprocess.run(
            ["buildah", "rm", container], cwd=root, check=False, capture_output=True, text=True
        )


@pytest.mark.integration
@pytest.mark.skipif(
    not nerdctl_available(), reason="working containerd and BuildKit services are unavailable"
)
def test_nerdctl_consumes_controller_selected_local_base() -> None:
    root = Path(__file__).parents[2]
    assert main(["images", "build", "curl", "--builder", "nerdctl"], cwd=root) == 0
    marker = subprocess.run(
        [
            "nerdctl",
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
