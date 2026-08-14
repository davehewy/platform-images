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


def test_invalid_name_with_build_file_is_rejected(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"Bad Name": "FROM scratch\n"})
    assert codes(root) == {"invalid-target-name"}


def test_non_image_directories_do_not_create_validation_errors(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory(
        {
            "api": "FROM scratch\n",
            "documentation": "<missing>",
            "generated-output": "<missing>",
        }
    )

    report = validate_repository(RepositoryConfig.load(root))

    assert report.valid
    assert list(report.graph.targets) == ["api"]


def test_containerfile_is_valid_but_both_build_filenames_are_ambiguous(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "<missing>"})
    directory = root / "images" / "base"
    (directory / "Containerfile").write_text("FROM alpine\n", encoding="utf-8")
    assert validate_repository(RepositoryConfig.load(root)).valid

    (directory / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
    report = validate_repository(RepositoryConfig.load(root))
    issue = next(issue for issue in report.errors if issue.code == "ambiguous-build-file")
    assert "exactly one" in (issue.hint or "")


def test_build_engine_configuration_is_validated(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'engine = "podman"', 'engine = "not-an-engine"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="not-an-engine"):
        RepositoryConfig.load(root)


def test_builder_and_registry_transport_are_independent_but_storage_compatible(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    contents = config_path.read_text(encoding="utf-8").replace(
        'engine = "podman"', 'backend = "buildah"'
    )
    contents = contents.replace('stable_tag = "main"', 'stable_tag = "main"\ntransport = "podman"')
    config_path.write_text(contents, encoding="utf-8")

    config = RepositoryConfig.load(root)

    assert config.build.backend.value == "buildah"
    assert config.registry.transport.value == "podman"


def test_incompatible_builder_and_registry_storage_are_rejected(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    contents = config_path.read_text(encoding="utf-8").replace(
        'engine = "podman"', 'backend = "nerdctl"'
    )
    contents = contents.replace('stable_tag = "main"', 'stable_tag = "main"\ntransport = "docker"')
    config_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="cannot use registry transport"):
        RepositoryConfig.load(root)


def test_discovery_root_is_repository_relative(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + '\n[discovery]\nroot = "../images"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="repository-relative"):
        RepositoryConfig.load(root)


def test_multiple_discovery_roots_load_and_resolve_cross_root_dependencies(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[discovery]\nroots = ["containers/shared", "services/api/images"]\n',
        encoding="utf-8",
    )
    base = root / "containers" / "shared" / "base"
    api = root / "services" / "api" / "images" / "api"
    base.mkdir(parents=True)
    api.mkdir(parents=True)
    (base / "Containerfile").write_text("FROM alpine\n", encoding="utf-8")
    (api / "Dockerfile").write_text("FROM base\n", encoding="utf-8")

    config = RepositoryConfig.load(root)
    report = validate_repository(config)

    assert config.discovery.roots == ("containers/shared", "services/api/images")
    assert report.valid
    assert report.graph.direct_dependencies("api") == ("base",)
    assert report.graph.targets["base"].context == base


def test_published_repository_joins_fully_qualified_reference_to_local_target(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory(
        {
            "ubuntu-base-24-04": "FROM alpine:3.22\n",
            "application": ("FROM nexus.example.com/gitlab/ubuntu-base-24-04:latest\n"),
        }
    )
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[images.ubuntu-base-24-04]\nrepository = "gitlab/ubuntu-base-24-04"\n',
        encoding="utf-8",
    )

    config = RepositoryConfig.load(root)
    report = validate_repository(config)

    assert report.valid
    assert config.image_repository("ubuntu-base-24-04") == "gitlab/ubuntu-base-24-04"
    assert report.graph.direct_dependencies("application") == ("ubuntu-base-24-04",)


def test_qualified_reference_with_exact_basename_is_local_without_image_configuration(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory(
        {
            "ubuntu24-04-base": "FROM alpine:3.22\n",
            "application": ("FROM nexus.example.com/gitlab-runner/ubuntu24-04-base:latest\n"),
        }
    )

    report = validate_repository(RepositoryConfig.load(root))

    assert report.valid
    assert not report.warnings
    assert report.graph.direct_dependencies("application") == ("ubuntu24-04-base",)


def test_qualified_near_match_warns_with_manual_mapping_configuration(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    source = "nexus.example.com/gitlab-runner/ubuntu24-base:latest"
    root = repository_factory(
        {
            "ubuntu24-04-base": "FROM alpine:3.22\n",
            "application": f"FROM {source}\n",
        }
    )

    report = validate_repository(RepositoryConfig.load(root))

    assert report.valid
    issue = next(issue for issue in report.warnings if issue.code == "probable-local-reference")
    assert "ubuntu24-04-base" in issue.message
    assert '[images."ubuntu24-04-base"]' in (issue.hint or "")
    assert 'aliases = ["nexus.example.com/gitlab-runner/ubuntu24-base"]' in (issue.hint or "")
    assert 'repository = "gitlab-runner/ubuntu24-base"' in (issue.hint or "")
    assert 'external_repositories = ["nexus.example.com/gitlab-runner/ubuntu24-base"]' in (
        issue.hint or ""
    )
    assert report.graph.direct_dependencies("application") == ()

    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[images."ubuntu24-04-base"]\n'
        + 'aliases = ["nexus.example.com/gitlab-runner/ubuntu24-base"]\n',
        encoding="utf-8",
    )
    mapped_report = validate_repository(RepositoryConfig.load(root))
    assert mapped_report.valid
    assert not mapped_report.warnings
    assert mapped_report.graph.direct_dependencies("application") == ("ubuntu24-04-base",)


def test_explicit_external_repository_prevents_automatic_local_edge(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    source = "docker.io/vendor/base"
    root = repository_factory(
        {
            "base": "FROM alpine:3.22\n",
            "application": f"FROM {source}:latest\n",
        }
    )
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f'\n[identity]\nexternal_repositories = ["{source}"]\n',
        encoding="utf-8",
    )

    report = validate_repository(RepositoryConfig.load(root))

    assert report.valid
    assert not report.warnings
    assert report.graph.direct_dependencies("application") == ()


def test_published_repository_alias_supports_a_legacy_remote_name(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory(
        {
            "base": "FROM alpine\n",
            "application": "FROM old-nexus.example.com:5000/legacy/base:main\n",
        }
    )
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + """
[images.base]
repository = "gitlab/base"
aliases = ["old-nexus.example.com:5000/legacy/base"]
""",
        encoding="utf-8",
    )

    report = validate_repository(RepositoryConfig.load(root))

    assert report.valid
    assert report.graph.direct_dependencies("application") == ("base",)


def test_duplicate_published_image_identity_is_rejected(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "legacy": "FROM busybox\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + """
[images.base]
repository = "gitlab/base"

[images.legacy]
aliases = ["nexus.example.com/gitlab/base"]
""",
        encoding="utf-8",
    )

    report = validate_repository(RepositoryConfig.load(root))

    issue = next(issue for issue in report.errors if issue.code == "duplicate-image-identity")
    assert "base, legacy" in issue.message


def test_stale_image_identity_configuration_is_rejected(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[images.removed]\nrepository = "gitlab/removed"\n',
        encoding="utf-8",
    )

    report = validate_repository(RepositoryConfig.load(root))

    assert {issue.code for issue in report.errors} == {"unknown-image-configuration"}


@pytest.mark.parametrize(
    "image_configuration",
    (
        '[images.base]\nrepository = "nexus.example.com/gitlab/base"\n',
        '[images.base]\nrepository = "gitlab/base:latest"\n',
        '[images.base]\naliases = ["nexus.example.com/gitlab/base:latest"]\n',
        '[images.base]\naliases = ["nexus.example.com/invalid/"]\n',
    ),
)
def test_invalid_published_image_configuration_is_rejected(
    repository_factory: Callable[[dict[str, str]], Path],
    image_configuration: str,
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n" + image_configuration,
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError):
        RepositoryConfig.load(root)


def test_legacy_single_discovery_root_remains_supported(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + '\n[discovery]\nroot = "containers"\n',
        encoding="utf-8",
    )
    (root / "containers").mkdir()

    config = RepositoryConfig.load(root)

    assert config.discovery.roots == ("containers",)
    assert config.discovery.root == "containers"


def test_discovery_root_and_roots_cannot_both_be_set(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[discovery]\nroot = "images"\nroots = ["containers"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="cannot both be set"):
        RepositoryConfig.load(root)


@pytest.mark.parametrize(
    "roots",
    (
        "[]",
        '["images", "images"]',
        '["../images"]',
    ),
)
def test_invalid_multiple_discovery_roots_are_rejected(
    repository_factory: Callable[[dict[str, str]], Path], roots: str
) -> None:
    root = repository_factory({})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + f"\n[discovery]\nroots = {roots}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError):
        RepositoryConfig.load(root)


def test_duplicate_target_names_across_roots_fail_with_both_paths(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[discovery]\nroots = ["containers/team-a", "containers/team-b"]\n',
        encoding="utf-8",
    )
    for discovery_root in ("containers/team-a", "containers/team-b"):
        target = root / discovery_root / "api"
        target.mkdir(parents=True)
        (target / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")

    report = validate_repository(RepositoryConfig.load(root))
    duplicate_paths = {
        issue.path for issue in report.errors if issue.code == "duplicate-target-name"
    }

    assert duplicate_paths == {"containers/team-a/api", "containers/team-b/api"}


def test_each_missing_discovery_root_is_reported(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[discovery]\nroots = ["missing-a", "missing-b"]\n',
        encoding="utf-8",
    )

    report = validate_repository(RepositoryConfig.load(root))

    assert {issue.path for issue in report.errors} == {"missing-a", "missing-b"}


def test_missing_configured_discovery_root_fails_closed(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[discovery]\nroot = "missing/container-images"\n',
        encoding="utf-8",
    )

    report = validate_repository(RepositoryConfig.load(root))

    assert {issue.code for issue in report.errors} == {"missing-discovery-root"}


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
    assert '[images."base"]' in (issue.hint or "")
    assert 'aliases = ["registry.example/platform-images/bsae"]' in (issue.hint or "")


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


@pytest.mark.parametrize(
    "value",
    (
        '"docker.io/vendor/base"',
        '["docker.io/vendor/base:latest"]',
        '["docker.io/vendor/base", "docker.io/vendor/base"]',
    ),
)
def test_explicit_external_repositories_are_strictly_validated(
    repository_factory: Callable[[dict[str, str]], Path], value: str
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f"\n[identity]\nexternal_repositories = {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError):
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
