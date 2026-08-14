from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from platform_images.models import ImageTarget

TARGET_NAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


@dataclass(frozen=True)
class TargetDiscovery:
    targets: dict[str, ImageTarget]
    entries: tuple[ImageTarget, ...]
    missing_roots: tuple[str, ...]


def inspect_targets(
    root: Path,
    discovery_roots: str | Path | Sequence[str | Path] = "images",
) -> TargetDiscovery:
    """Discover direct children across all configured roots without hiding collisions."""
    roots = (
        (discovery_roots,) if isinstance(discovery_roots, str | Path) else tuple(discovery_roots)
    )
    root_paths = tuple(sorted(Path(value) for value in roots))
    targets: dict[str, ImageTarget] = {}
    entries: list[ImageTarget] = []
    missing_roots: list[str] = []
    for discovery_root in root_paths:
        target_root = root / discovery_root
        if not target_root.is_dir():
            missing_roots.append(discovery_root.as_posix())
            continue
        for directory in sorted(
            (entry for entry in target_root.iterdir() if entry.is_dir()),
            key=lambda path: path.name,
        ):
            dockerfile = directory / "Dockerfile"
            containerfile = directory / "Containerfile"
            if not (dockerfile.is_file() or containerfile.is_file()):
                continue
            build_file = (
                dockerfile if dockerfile.is_file() or not containerfile.is_file() else containerfile
            )
            target = ImageTarget(
                name=directory.name,
                directory=directory,
                dockerfile=build_file,
                context=directory,
            )
            entries.append(target)
            targets.setdefault(target.name, target)
    return TargetDiscovery(
        dict(sorted(targets.items())),
        tuple(entries),
        tuple(missing_roots),
    )


def discover_targets(
    root: Path,
    discovery_roots: str | Path | Sequence[str | Path] = "images",
) -> dict[str, ImageTarget]:
    """Discover logical image targets across one or more repository-relative roots."""
    return inspect_targets(root, discovery_roots).targets


def valid_target_name(name: str) -> bool:
    return TARGET_NAME_RE.fullmatch(name) is not None
