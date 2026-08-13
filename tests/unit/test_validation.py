from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from platform_images.config import RepositoryConfig
from platform_images.errors import ConfigurationError
from platform_images.validation import validate_repository


def codes(root: Path) -> set[str]:
    return {issue.code for issue in validate_repository(RepositoryConfig.load(root)).errors}


def test_valid_repository(repository_factory: Callable[[dict[str, str]], Path]) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    report = validate_repository(RepositoryConfig.load(root))
    assert report.valid
    assert not report.warnings


def test_invalid_name_and_missing_dockerfile(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"Bad Name": "<missing>"})
    assert codes(root) == {"invalid-target-name", "missing-dockerfile"}


def test_unresolved_reference_has_stable_code(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"api": "ARG BASE\nFROM ${BASE}\n"})
    assert "unresolved-reference" in codes(root)


def test_internal_typo_has_hint(repository_factory: Callable[[dict[str, str]], Path]) -> None:
    root = repository_factory(
        {
            "base": "FROM alpine\n",
            "curl": "FROM registry.example/platform-images/bsae:main\n",
        }
    )
    report = validate_repository(RepositoryConfig.load(root))
    issue = next(issue for issue in report.errors if issue.code == "missing-internal-target")
    assert issue.hint == "possible match: base"


def test_unqualified_local_typo_is_rejected_with_hint(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM bsae\n"})
    report = validate_repository(RepositoryConfig.load(root))
    issue = next(issue for issue in report.errors if issue.code == "unknown-short-reference")
    assert issue.hint == "possible local target: base"


def test_unqualified_external_requires_explicit_allowlist(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM debian:bookworm\n"})
    assert "unknown-short-reference" in codes(root)


def test_stage_target_collision_is_rejected(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM alpine AS base\nFROM base\n"})
    assert "stage-target-collision" in codes(root)


def test_cycle_is_rejected_with_path(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM curl\n", "curl": "FROM base\n"})
    report = validate_repository(RepositoryConfig.load(root))
    issue = next(issue for issue in report.errors if issue.code == "dependency-cycle")
    assert "base -> curl -> base" in issue.message


def test_invalid_registry_namespace_has_stable_code(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'namespace = "platform-images"', 'namespace = "Invalid Namespace"'
        ),
        encoding="utf-8",
    )
    assert "invalid-registry-namespace" in codes(root)


def test_short_external_allowlist_must_be_an_array(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'allowed_short_external_images = ["alpine", "busybox"]',
            'allowed_short_external_images = "alpine"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="must be an array of strings"):
        RepositoryConfig.load(root)


def test_unterminated_dockerfile_construct_is_a_validation_error(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\nRUN <<EOF\necho unfinished\n"})
    assert "invalid-dockerfile-syntax" in codes(root)


def test_external_symlink_is_rejected_without_parsing_external_dockerfile(
    repository_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    external = tmp_path / "external-image"
    external.mkdir()
    (external / "Dockerfile").write_text("FROM bsae\n", encoding="utf-8")
    (root / "images" / "escaped").symlink_to(external, target_is_directory=True)

    report = validate_repository(RepositoryConfig.load(root))

    assert "path-outside-repository" in {issue.code for issue in report.errors}
    assert "unknown-short-reference" not in {issue.code for issue in report.errors}
