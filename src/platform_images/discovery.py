from __future__ import annotations

import re
from pathlib import Path

from platform_images.models import ImageTarget

TARGET_NAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def discover_targets(root: Path) -> dict[str, ImageTarget]:
    """Discover every direct child of images/ without a central registration list."""
    images_dir = root / "images"
    if not images_dir.is_dir():
        return {}
    targets: dict[str, ImageTarget] = {}
    for directory in sorted(
        (entry for entry in images_dir.iterdir() if entry.is_dir()), key=lambda path: path.name
    ):
        dockerfile = directory / "Dockerfile"
        containerfile = directory / "Containerfile"
        build_file = (
            dockerfile if dockerfile.exists() or not containerfile.exists() else containerfile
        )
        targets[directory.name] = ImageTarget(
            name=directory.name,
            directory=directory,
            dockerfile=build_file,
            context=directory,
        )
    return targets


def valid_target_name(name: str) -> bool:
    return TARGET_NAME_RE.fullmatch(name) is not None
