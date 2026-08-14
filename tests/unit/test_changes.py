from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import git

from platform_images.changes import (
    GitClient,
    detect_changes,
    map_changes,
    parse_name_status,
    validate_removed_references,
)
from platform_images.config import RepositoryConfig
from platform_images.discovery import discover_targets
from platform_images.errors import GitError, PlatformImagesError
from platform_images.graph import build_graph
from platform_images.models import BuildMode, ChangedPath
from platform_images.planner import change_plan
from platform_images.registry import StaticRegistryClient
from platform_images.renderers.gitlab import render_gitlab


def test_parse_nul_statuses_including_rename_copy_and_space() -> None:
    parsed = parse_name_status(
        "M\0images/curl/file with space\0R100\0images/old/Dockerfile\0"
        "images/new/Dockerfile\0C075\0a\0b\0D\0images/gone/Dockerfile\0"
    )
    assert parsed == (
        ChangedPath("C075", "a", "b"),
        ChangedPath("M", None, "images/curl/file with space"),
        ChangedPath("D", "images/gone/Dockerfile", None),
        ChangedPath("R100", "images/old/Dockerfile", "images/new/Dockerfile"),
    )


def test_path_mapping_global_inputs_and_removed_target(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    config = RepositoryConfig.load(root)
    changes = map_changes(
        (
            ChangedPath("M", None, "images/curl/files/config"),
            ChangedPath("D", "images/gone/Dockerfile", None),
            ChangedPath("D", "images/also-gone/Containerfile", None),
            ChangedPath("M", None, "docs/readme.md"),
        ),
        discover_targets(root),
        config,
    )
    assert changes.changed_targets == {"curl"}
    assert changes.removed_targets == {"also-gone", "gone"}
    assert not changes.global_change

    global_changes = map_changes(
        (ChangedPath("M", None, "src/platform_images/graph.py"),),
        discover_targets(root),
        config,
    )
    assert global_changes.global_change
    assert global_changes.changed_targets == {"base", "curl"}

    github_workflow_changes = map_changes(
        (ChangedPath("M", None, ".github/workflows/container-images.yml"),),
        discover_targets(root),
        config,
    )
    assert github_workflow_changes.global_change
    assert github_workflow_changes.changed_targets == {"base", "curl"}


def test_path_mapping_uses_the_configured_discovery_root(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"api": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[discovery]\nroot = "deploy/container-images"\n',
        encoding="utf-8",
    )
    config = RepositoryConfig.load(root)

    changes = map_changes(
        (ChangedPath("M", None, "deploy/container-images/api/Dockerfile"),),
        {"api": object()},
        config,
    )

    assert changes.changed_targets == {"api"}
    assert changes.reasons == {"api": ("source-changed:deploy/container-images/api/Dockerfile",)}


def test_path_mapping_uses_every_configured_discovery_root(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[discovery]\nroots = ["containers/shared", "services/payments/images"]\n',
        encoding="utf-8",
    )
    config = RepositoryConfig.load(root)

    changes = map_changes(
        (
            ChangedPath("M", None, "containers/shared/base/files/config"),
            ChangedPath("M", None, "services/payments/images/api/Dockerfile"),
            ChangedPath("D", "services/payments/images/retired/Containerfile", None),
        ),
        {"api": object(), "base": object()},
        config,
    )

    assert changes.changed_targets == {"api", "base"}
    assert changes.removed_targets == {"retired"}
    assert changes.reasons == {
        "api": ("source-changed:services/payments/images/api/Dockerfile",),
        "base": ("source-changed:containers/shared/base/files/config",),
    }


def test_path_mapping_marks_nested_and_containing_image_contexts(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[discovery]\nroots = ["containers", "containers/service/images"]\n',
        encoding="utf-8",
    )
    config = RepositoryConfig.load(root)

    changes = map_changes(
        (
            ChangedPath("M", None, "containers/service/images/api/source/application.py"),
            ChangedPath("D", "containers/service/images/retired/Dockerfile", None),
        ),
        {"api": object(), "service": object()},
        config,
    )

    assert changes.changed_targets == {"api", "service"}
    assert changes.removed_targets == {"retired"}
    assert changes.reasons == {
        "api": ("source-changed:containers/service/images/api/source/application.py",),
        "service": (
            "source-changed:containers/service/images/api/source/application.py",
            "source-changed:containers/service/images/retired/Dockerfile",
        ),
    }


def test_removed_target_in_nested_root_does_not_invent_an_ancestor_target(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[discovery]\nroots = ["utils", "utils/container-images"]\n',
        encoding="utf-8",
    )

    changes = map_changes(
        (ChangedPath("D", "utils/container-images/retired/Dockerfile", None),),
        {"api": object()},
        RepositoryConfig.load(root),
    )

    assert changes.changed_targets == set()
    assert changes.removed_targets == {"retired"}


def test_real_git_modify_add_rename_remove_and_spaces(git_repository: Path) -> None:
    root = git_repository
    config = RepositoryConfig.load(root)
    base = git(root, "rev-parse", "HEAD")
    (root / "images" / "curl" / "file with space").write_text("x", encoding="utf-8")
    (root / "images" / "jq").mkdir()
    (root / "images" / "jq" / "Dockerfile").write_text("FROM base\n", encoding="utf-8")
    git(root, "mv", "images/base", "images/foundation")
    (root / "images" / "foundation" / "Dockerfile").write_text(
        "FROM alpine:3.22\n", encoding="utf-8"
    )
    (root / "images" / "curl" / "Dockerfile").write_text("FROM foundation\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "change topology")
    head = git(root, "rev-parse", "HEAD")

    changes = detect_changes(config, discover_targets(root), base, head)

    assert changes.changed_targets == {"curl", "foundation", "jq"}
    assert changes.removed_targets == {"base"}
    statuses = {item.status[:1] for item in changes.changed_files}
    assert {"A", "M", "R"}.issubset(statuses)
    assert any("file with space" in path for item in changes.changed_files for path in item.paths)


def test_docs_only_is_not_relevant(git_repository: Path) -> None:
    base = git(git_repository, "rev-parse", "HEAD")
    (git_repository / "docs").mkdir()
    (git_repository / "docs" / "notes.md").write_text("notes", encoding="utf-8")
    git(git_repository, "add", ".")
    git(git_repository, "commit", "-qm", "docs")
    head = git(git_repository, "rev-parse", "HEAD")
    changes = detect_changes(
        RepositoryConfig.load(git_repository),
        discover_targets(git_repository),
        base,
        head,
    )
    assert not changes.changed_targets


def test_unavailable_base_fails_actionably(git_repository: Path) -> None:
    with pytest.raises(GitError, match="unable to compare Git commits"):
        GitClient(git_repository).changed_paths("not-a-commit", "HEAD")


def test_real_git_modify_curl_and_rename_its_file(git_repository: Path) -> None:
    root = git_repository
    install = root / "images" / "curl" / "install script.sh"
    install.write_text("original", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "add install script")
    base = git(root, "rev-parse", "HEAD")
    git(root, "mv", "images/curl/install script.sh", "images/curl/install.sh")
    (root / "images" / "curl" / "Dockerfile").write_text(
        "FROM base\nRUN echo modified\n", encoding="utf-8"
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "modify curl and rename input")
    head = git(root, "rev-parse", "HEAD")

    changes = detect_changes(RepositoryConfig.load(root), discover_targets(root), base, head)

    assert changes.changed_targets == {"curl"}
    assert any(item.status.startswith("R") for item in changes.changed_files)


def test_real_git_remove_unused_image(git_repository: Path) -> None:
    root = git_repository
    unused = root / "images" / "unused"
    unused.mkdir()
    (unused / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "add unused")
    base = git(root, "rev-parse", "HEAD")
    git(root, "rm", "images/unused/Dockerfile")
    git(root, "commit", "-qm", "remove unused")
    head = git(root, "rev-parse", "HEAD")

    changes = detect_changes(RepositoryConfig.load(root), discover_targets(root), base, head)

    assert changes.changed_targets == frozenset()
    assert changes.removed_targets == {"unused"}


def test_real_git_controller_change_marks_every_image(git_repository: Path) -> None:
    root = git_repository
    base = git(root, "rev-parse", "HEAD")
    controller = root / "src" / "platform_images"
    controller.mkdir(parents=True)
    (controller / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "controller change")
    head = git(root, "rev-parse", "HEAD")

    changes = detect_changes(RepositoryConfig.load(root), discover_targets(root), base, head)

    assert changes.global_change
    assert changes.changed_targets == {"base", "curl"}


def test_mandatory_global_input_cannot_be_disabled_by_same_commit(
    git_repository: Path,
) -> None:
    root = git_repository
    base = git(root, "rev-parse", "HEAD")
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace('  "platform-images.toml",\n', ""),
        encoding="utf-8",
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "try to disable global detection")
    head = git(root, "rev-parse", "HEAD")

    changes = detect_changes(RepositoryConfig.load(root), discover_targets(root), base, head)

    assert changes.global_change
    assert changes.changed_targets == {"base", "curl"}


def test_added_jq_is_discovered_planned_and_rendered(git_repository: Path) -> None:
    root = git_repository
    base = git(root, "rev-parse", "HEAD")
    jq = root / "images" / "jq"
    jq.mkdir()
    (jq / "Dockerfile").write_text("FROM base\nRUN apk add --no-cache jq\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "add jq")
    head = git(root, "rev-parse", "HEAD")
    config = RepositoryConfig.load(root)
    graph = build_graph(discover_targets(root), config)
    changes = detect_changes(config, graph.targets, base, head)

    assert "jq" in graph.targets
    plan = change_plan(
        graph,
        config,
        changes,
        base_sha=base,
        head_sha=head,
        mode=BuildMode.MERGE_REQUEST,
        registry="registry.example.com",
        pipeline_id="88",
        registry_client=StaticRegistryClient(
            {"base": "registry.example.com/platform-images/base@sha256:stable"}
        ),
    )
    child = render_gitlab(plan, config, "registry.example.com")
    assert [target.name for target in plan.targets] == ["jq"]
    assert "image_jq:" in child


def test_change_json_shape_is_serializable(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"curl": "FROM alpine\n"})
    changes = map_changes(
        (ChangedPath("T", None, "images/curl/Dockerfile"),),
        discover_targets(root),
        RepositoryConfig.load(root),
    )
    assert json.dumps(sorted(changes.changed_targets)) == '["curl"]'


def test_surviving_reference_to_removed_target_fails(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"curl": "FROM base\n"})
    config = RepositoryConfig.load(root)
    graph = build_graph(discover_targets(root), config)
    changes = map_changes(
        (ChangedPath("D", "images/base/Dockerfile", None),), graph.targets, config
    )
    with pytest.raises(PlatformImagesError, match="still references removed local image target"):
        validate_removed_references(graph, changes, config.root)


def test_deleting_an_unrelated_file_does_not_invent_removed_target(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"curl": "FROM alpine\n"})
    config = RepositoryConfig.load(root)
    changes = map_changes(
        (ChangedPath("D", "images/old/readme.txt", None),),
        discover_targets(root),
        config,
    )
    assert not changes.removed_targets
