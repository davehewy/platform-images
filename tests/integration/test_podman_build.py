from __future__ import annotations

import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest

from platform_images.cli import main
from platform_images.config import RepositoryConfig
from platform_images.references import ReferencePolicy


def _command_succeeds(arguments: list[str]) -> bool:
    if shutil.which(arguments[0]) is None:
        return False
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@cache
def podman_available() -> bool:
    return _command_succeeds(["podman", "info"])


@cache
def docker_available() -> bool:
    return _command_succeeds(["docker", "info"]) and _command_succeeds(
        ["docker", "buildx", "version"]
    )


@cache
def buildah_available() -> bool:
    return _command_succeeds(["buildah", "info"])


@cache
def nerdctl_available() -> bool:
    return shutil.which("buildctl") is not None and _command_succeeds(["nerdctl", "info"])


@pytest.mark.integration
@pytest.mark.parametrize(
    ("builder", "available"),
    (
        ("docker", docker_available),
        ("podman", podman_available),
        ("buildah", buildah_available),
        ("nerdctl", nerdctl_available),
    ),
)
def test_fully_qualified_local_parent_is_replaced_by_planned_image(
    tmp_path: Path,
    builder: str,
    available,
) -> None:
    if not available():
        pytest.skip(f"a working {builder} setup is unavailable")
    root = tmp_path / f"qualified-reference-{builder}"
    base = root / "containers" / "base"
    application = root / "containers" / "application"
    base.mkdir(parents=True)
    application.mkdir(parents=True)
    (base / "Dockerfile").write_text(
        "FROM alpine:3.22\nRUN echo exact-local-parent > /parent-marker\n",
        encoding="utf-8",
    )
    (application / "Dockerfile").write_text(
        "ARG SOURCE_REGISTRY\n"
        "FROM ${SOURCE_REGISTRY}/base:latest\n"
        'RUN test "$(cat /parent-marker)" = exact-local-parent\n',
        encoding="utf-8",
    )

    assert (
        main(
            [
                "init",
                "--builder",
                builder,
                "--build-arg",
                "SOURCE_REGISTRY=registry.invalid/gitlab",
            ],
            cwd=root,
        )
        == 0
    )
    assert main(["images", "build", "application"], cwd=root) == 0


@pytest.mark.integration
def test_curl_consumes_controller_selected_local_base() -> None:
    if not podman_available():
        pytest.skip("a working Podman service is unavailable")
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
def test_docker_buildx_consumes_controller_selected_local_base() -> None:
    if not docker_available():
        pytest.skip("a working Docker Buildx service is unavailable")
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
def test_generated_bake_builds_an_argument_bound_parent_chain(tmp_path: Path) -> None:
    if not docker_available():
        pytest.skip("a working Docker Buildx service is unavailable")
    root = tmp_path / "qualified-bake"
    base = root / "containers" / "base.image"
    application = root / "containers" / "application"
    base.mkdir(parents=True)
    application.mkdir(parents=True)
    (base / "Dockerfile").write_text(
        "FROM alpine:3.22\nRUN echo exact-bake-parent > /parent-marker\n",
        encoding="utf-8",
    )
    (application / "Dockerfile").write_text(
        "ARG SOURCE_REGISTRY\n"
        "FROM ${SOURCE_REGISTRY}/base.image:latest\n"
        'RUN test "$(cat /parent-marker)" = exact-bake-parent\n',
        encoding="utf-8",
    )

    assert (
        main(
            [
                "init",
                "--builder",
                "docker",
                "--build-arg",
                "SOURCE_REGISTRY=registry.invalid/gitlab",
            ],
            cwd=root,
        )
        == 0
    )
    bake_file = root / "docker-bake.hcl"
    assert (
        main(
            [
                "images",
                "generate-bake",
                "--image",
                "application",
                "--output",
                bake_file.name,
            ],
            cwd=root,
        )
        == 0
    )
    printed = subprocess.run(
        ["docker", "buildx", "bake", "--file", str(bake_file), "--print"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert printed.returncode == 0, printed.stderr
    built = subprocess.run(
        ["docker", "buildx", "bake", "--file", str(bake_file), "--load"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert built.returncode == 0, built.stderr

    output_ref = ReferencePolicy(RepositoryConfig.load(root)).local("application")
    marker = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            output_ref,
            "-c",
            'test "$(cat /parent-marker)" = exact-bake-parent',
        ],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert marker.returncode == 0, marker.stderr


@pytest.mark.integration
def test_buildah_consumes_controller_selected_local_base() -> None:
    if not buildah_available():
        pytest.skip("a working Buildah setup is unavailable")
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
def test_nerdctl_consumes_controller_selected_local_base() -> None:
    if not nerdctl_available():
        pytest.skip("working containerd and BuildKit services are unavailable")
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
