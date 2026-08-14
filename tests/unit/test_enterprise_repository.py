from __future__ import annotations

from pathlib import Path

from conftest import git

from platform_images.changes import detect_changes
from platform_images.config import RepositoryConfig
from platform_images.discovery import discover_targets
from platform_images.models import BuildMode
from platform_images.planner import change_plan
from platform_images.registry import StaticRegistryClient
from platform_images.validation import validate_repository

ROOTS = (
    "containers/shared",
    "services/core/container-images",
    "platform/build/images",
    "utils/container-images",
)


def _target_name(index: int) -> str:
    return f"image-{index:03d}"


def _parent_reference(index: int) -> tuple[str, str | None]:
    parent = _target_name(index - 1)
    if index == 21:
        return "nexus.example.com/legacy/foundation-base:latest", None
    if index % 3 == 0:
        return f"${{SOURCE_REGISTRY}}/{parent}:latest", "ARG SOURCE_REGISTRY\n"
    if index % 3 == 1:
        return f"nexus.example.com/platform/base-images/{parent}:latest", None
    return f"ghcr.io/acme-platform/{parent}@sha256:{'a' * 64}", None


def _enterprise_repository(tmp_path: Path, image_count: int = 100) -> Path:
    root = tmp_path / "enterprise-monorepo"
    root.mkdir()
    (root / "platform-images.toml").write_text(
        """\
[registry]
namespace = "platform/base-images"
registry_environment_variable = "PLATFORM_IMAGES_REGISTRY"
stable_tag = "main"
transport = "docker"
provider = "oci"
authentication = "ambient"

[tags]
ci_prefix = "ci"
commit_prefix = "sha"

[build]
backend = "docker"

[dockerfile]
allowed_short_external_images = ["alpine"]
arguments = { SOURCE_REGISTRY = "registry.gitlab.example/platform/base-images" }

[identity]
external_repositories = ["ghcr.io/vendor/image-050"]

[images."image-020"]
aliases = ["nexus.example.com/legacy/foundation-base"]

[changes]
global_inputs = ["build/policy/**"]

[discovery]
roots = [
  "containers/shared",
  "services/core/container-images",
  "platform/build/images",
  "utils/container-images",
]
""",
        encoding="utf-8",
    )
    for index in range(image_count):
        directory = root / ROOTS[index % len(ROOTS)] / _target_name(index)
        directory.mkdir(parents=True)
        if index == 0:
            dockerfile = "FROM alpine:3.22\n"
        else:
            reference, prefix = _parent_reference(index)
            dockerfile = f"{prefix or ''}FROM {reference}\n"
        if index == image_count - 1:
            dockerfile += (
                "COPY --from=ghcr.io/vendor/image-050:latest /usr/bin/tool /usr/bin/tool\n"
            )
        (directory / ("Containerfile" if index % 2 else "Dockerfile")).write_text(
            dockerfile,
            encoding="utf-8",
        )
    return root


def test_hundred_image_enterprise_fixture_resolves_and_rebuilds_complete_chain(
    tmp_path: Path,
) -> None:
    root = _enterprise_repository(tmp_path)
    config = RepositoryConfig.load(root)
    report = validate_repository(config)

    assert report.valid
    assert not report.warnings
    assert len(report.graph.targets) == 100
    assert report.graph.direct_dependencies("image-021") == ("image-020",)
    assert report.graph.direct_dependencies("image-099") == ("image-098",)
    assert report.graph.topological_order() == tuple(_target_name(index) for index in range(100))
    arg_reference = next(
        reference
        for reference in report.graph.parse_results["image-003"].references
        if reference.resolved == "image-002"
    )
    assert arg_reference.source == ("registry.gitlab.example/platform/base-images/image-002:latest")

    git(root, "init", "-q")
    git(root, "config", "user.email", "tests@example.com")
    git(root, "config", "user.name", "Tests")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    base_sha = git(root, "rev-parse", "HEAD")
    first = discover_targets(root, config.discovery.roots)["image-000"].dockerfile
    first.write_text(first.read_text(encoding="utf-8") + "RUN echo changed\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "change foundation")
    head_sha = git(root, "rev-parse", "HEAD")

    changes = detect_changes(config, report.graph.targets, base_sha, head_sha)
    plan = change_plan(
        report.graph,
        config,
        changes,
        base_sha=base_sha,
        head_sha=head_sha,
        mode=BuildMode.DEFAULT_BRANCH,
        registry="nexus.example.com",
        pipeline_id="100",
        registry_client=StaticRegistryClient({}),
    )

    assert changes.changed_targets == {"image-000"}
    assert len(plan.targets) == 100
    assert plan.targets[0].name == "image-000"
    assert plan.targets[-1].name == "image-099"
