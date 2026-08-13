from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

CONFIG = """\
[registry]
namespace = "platform-images"
registry_environment_variable = "PLATFORM_IMAGES_REGISTRY"
stable_tag = "main"

[tags]
ci_prefix = "ci"
commit_prefix = "sha"

[dockerfile]
allowed_short_external_images = ["alpine", "busybox"]

[changes]
global_inputs = [
  "src/platform_images/**",
  "pyproject.toml",
  "platform-images.toml",
  ".gitlab-ci.yml",
  ".gitlab/**",
]
"""


@pytest.fixture
def repository_factory(tmp_path: Path) -> Callable[[dict[str, str]], Path]:
    counter = 0

    def create(images: dict[str, str]) -> Path:
        nonlocal counter
        counter += 1
        root = tmp_path / f"repository-{counter}"
        root.mkdir()
        (root / "platform-images.toml").write_text(CONFIG, encoding="utf-8")
        for name, dockerfile in images.items():
            directory = root / "images" / name
            directory.mkdir(parents=True)
            if dockerfile != "<missing>":
                (directory / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        return root

    return create


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def git_repository(repository_factory: Callable[[dict[str, str]], Path]) -> Path:
    root = repository_factory(
        {
            "base": "FROM alpine:3.22\n",
            "curl": "FROM base\nRUN echo curl\n",
        }
    )
    git(root, "init", "-q")
    git(root, "config", "user.email", "tests@example.com")
    git(root, "config", "user.name", "Tests")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root
