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


def test_discovers_containerfile_when_dockerfile_is_absent(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "<missing>"})
    containerfile = root / "images" / "base" / "Containerfile"
    containerfile.write_text("FROM scratch\n", encoding="utf-8")

    assert discover_targets(root)["base"].dockerfile == containerfile


def test_discovers_targets_below_a_configurable_nested_root(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    target = root / "deploy" / "container-images" / "api"
    target.mkdir(parents=True)
    (target / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")

    targets = discover_targets(root, "deploy/container-images")

    assert list(targets) == ["api"]
    assert targets["api"].context == target
    assert targets["api"].dockerfile == target / "Containerfile"


def test_target_name_rules() -> None:
    assert valid_target_name("new-tool")
    assert valid_target_name("python_3.12")
    assert not valid_target_name("BadName")
    assert not valid_target_name("two--separators")
    assert not valid_target_name("../escape")
