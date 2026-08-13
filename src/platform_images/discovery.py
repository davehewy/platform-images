from __future__ import annotations

import re
from pathlib import Path

from platform_images.models import ImageTarget

TARGET_NAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def discover_targets(root: Path, discovery_root: str | Path = "images") -> dict[str, ImageTarget]:
    """Discover every direct child of the configured target root."""
    target_root = root / discovery_root
    if not target_root.is_dir():
        return {}
    targets: dict[str, ImageTarget] = {}
    for directory in sorted(
        (entry for entry in target_root.iterdir() if entry.is_dir()), key=lambda path: path.name
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
