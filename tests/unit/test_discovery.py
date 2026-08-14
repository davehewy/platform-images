from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from platform_images.discovery import discover_targets, inspect_targets, valid_target_name


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


def test_discovers_targets_across_multiple_roots(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    base = root / "containers" / "shared" / "base"
    api = root / "services" / "api" / "container" / "api"
    base.mkdir(parents=True)
    api.mkdir(parents=True)
    (base / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
    (api / "Dockerfile").write_text("FROM base\n", encoding="utf-8")

    discovery = inspect_targets(
        root,
        ("services/api/container", "containers/shared"),
    )

    assert list(discovery.targets) == ["api", "base"]
    assert discovery.targets["api"].context == api
    assert discovery.targets["base"].context == base
    assert not discovery.missing_roots


def test_inspection_preserves_duplicate_entries_across_roots(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    for discovery_root in ("containers/team-a", "containers/team-b"):
        target = root / discovery_root / "api"
        target.mkdir(parents=True)
        (target / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    discovery = inspect_targets(root, ("containers/team-a", "containers/team-b"))

    assert list(discovery.targets) == ["api"]
    assert [entry.name for entry in discovery.entries] == ["api", "api"]


def test_nested_root_branch_is_not_mistaken_for_an_image_target(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    tool = root / "utils" / "tool"
    api = root / "utils" / "container-images" / "api"
    tool.mkdir(parents=True)
    api.mkdir(parents=True)
    (tool / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (api / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    discovery = inspect_targets(root, ("utils", "utils/container-images"))

    assert list(discovery.targets) == ["api", "tool"]
    assert [entry.name for entry in discovery.entries] == ["tool", "api"]


def test_outer_target_with_build_file_is_retained_alongside_nested_root(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    outer = root / "containers" / "service"
    api = outer / "images" / "api"
    api.mkdir(parents=True)
    (outer / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (api / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    discovery = inspect_targets(root, ("containers", "containers/service/images"))

    assert list(discovery.targets) == ["api", "service"]
    assert {entry.directory for entry in discovery.entries} == {outer, api}


def test_target_name_rules() -> None:
    assert valid_target_name("new-tool")
    assert valid_target_name("python_3.12")
    assert not valid_target_name("BadName")
    assert not valid_target_name("two--separators")
    assert not valid_target_name("../escape")
