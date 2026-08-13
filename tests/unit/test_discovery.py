from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from platform_images.discovery import discover_targets, valid_target_name


def test_discovers_directories_without_registration(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"zulu": "FROM scratch\n", "alpha": "FROM scratch\n"})

    targets = discover_targets(root)

    assert list(targets) == ["alpha", "zulu"]
    assert targets["alpha"].context == root / "images" / "alpha"
    assert targets["alpha"].dockerfile == root / "images" / "alpha" / "Dockerfile"


def test_target_name_rules() -> None:
    assert valid_target_name("new-tool")
    assert valid_target_name("python_3.12")
    assert not valid_target_name("BadName")
    assert not valid_target_name("two--separators")
    assert not valid_target_name("../escape")
