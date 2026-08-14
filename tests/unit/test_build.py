from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from platform_images.build import (
    _oci_layout_reference,
    build_command,
    execute_ci_build,
    execute_local_plan,
    parse_input_references,
)
from platform_images.config import RepositoryConfig
from platform_images.discovery import discover_targets
from platform_images.errors import PlatformImagesError, ProcessError
from platform_images.graph import build_graph
from platform_images.models import (
    BuildBackend,
    BuildPlanTarget,
    RegistryTransport,
)
from platform_images.planner import local_plan
from platform_images.process import ProcessResult

EXISTING_DIGEST = "sha256:" + "e" * 64
PODMAN_DIGEST = "sha256:" + "d" * 64
DOCKER_DIGEST = "sha256:" + "c" * 64


class RecordingRunner:
    def __init__(
        self,
        *,
        fail_push: bool = False,
        existing_labels: Mapping[str, str] | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.fail_push = fail_push
        self.existing_labels = existing_labels
        self.pushed_labels: dict[str, str] | None = None

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture_output: bool = False,
    ) -> ProcessResult:
        del cwd, env, capture_output
        command = list(arguments)
        self.commands.append(command)
        if (
            len(command) >= 2
            and command[0] in {"podman", "docker", "nerdctl", "buildah"}
            and command[1] == "pull"
        ):
            if self.existing_labels is None and self.pushed_labels is None:
                raise ProcessError("image not found")
            return ProcessResult("", "", 0)
        if (
            len(command) >= 3
            and command[0] in {"podman", "docker", "nerdctl"}
            and command[1:3] == ["image", "inspect"]
        ):
            return ProcessResult(
                json.dumps(
                    [
                        {
                            "Digest": EXISTING_DIGEST,
                            "Labels": dict(self.existing_labels or self.pushed_labels or {}),
                            "RepoDigests": [f"registry/image@{EXISTING_DIGEST}"],
                        }
                    ]
                ),
                "",
                0,
            )
        if command[:3] == ["buildah", "inspect", "--type"]:
            return ProcessResult(
                json.dumps(
                    {
                        "FromImageDigest": EXISTING_DIGEST,
                        "Docker": {
                            "config": {
                                "Labels": dict(self.existing_labels or self.pushed_labels or {})
                            }
                        },
                    }
                ),
                "",
                0,
            )
        if command[:2] in (["podman", "push"], ["buildah", "push"]):
            if self.fail_push:
                raise ProcessError("push failed")
            digest_path = Path(command[command.index("--digestfile") + 1])
            digest_path.write_text(PODMAN_DIGEST + "\n", encoding="utf-8")
        if command[:4] == ["docker", "buildx", "build", "--push"]:
            if self.fail_push:
                raise ProcessError("push failed")
            metadata_path = Path(command[command.index("--metadata-file") + 1])
            metadata_path.write_text(
                json.dumps({"containerimage.digest": DOCKER_DIGEST}),
                encoding="utf-8",
            )
        if command[:2] == ["nerdctl", "build"] and "--output" in command:
            if self.fail_push:
                raise ProcessError("push failed")
            self.pushed_labels = {
                value.partition("=")[0]: value.partition("=")[2]
                for index, value in enumerate(command)
                if index > 0 and command[index - 1] == "--label"
            }
        if command[:2] == ["nerdctl", "save"]:
            archive_path = Path(command[command.index("--output") + 1])
            reference = command[-1]
            index = json.dumps(
                {
                    "schemaVersion": 2,
                    "manifests": [
                        {
                            "mediaType": "application/vnd.oci.image.manifest.v1+json",
                            "digest": "sha256:" + "a" * 64,
                            "annotations": {"org.opencontainers.image.ref.name": reference},
                        }
                    ],
                }
            ).encode()
            layout = b'{"imageLayoutVersion":"1.0.0"}'
            with tarfile.open(archive_path, "w") as archive:
                for name, contents in (("index.json", index), ("oci-layout", layout)):
                    info = tarfile.TarInfo(name)
                    info.size = len(contents)
                    archive.addfile(info, io.BytesIO(contents))
        return ProcessResult("", "", 0)


def test_build_command_uses_sorted_named_contexts_and_argument_list() -> None:
    target = BuildPlanTarget(
        "app",
        ("selected",),
        ("a", "z"),
        ("a", "z"),
        "images/app/Dockerfile",
        "images/app",
        "localhost/platform-images/app:dev",
        {"z": "registry/z@sha256:z", "a": "registry/a@sha256:a"},
        False,
    )
    command = build_command(target)
    assert command == [
        "podman",
        "build",
        "--build-context",
        "a=container-image://registry/a@sha256:a",
        "--build-context",
        "z=container-image://registry/z@sha256:z",
        "--file",
        "images/app/Dockerfile",
        "--tag",
        "localhost/platform-images/app:dev",
        "images/app",
    ]


def test_build_command_replaces_fully_qualified_dockerfile_image_source() -> None:
    planned_base = "localhost/gitlab/ubuntu-base-24-04:dev"
    target = BuildPlanTarget(
        "application",
        ("selected",),
        ("ubuntu-base-24-04",),
        ("ubuntu-base-24-04",),
        "images/application/Dockerfile",
        "images/application",
        "localhost/gitlab/application:dev",
        {"ubuntu-base-24-04": planned_base},
        False,
        {
            "nexus.example.com/gitlab/ubuntu-base-24-04:latest": planned_base,
            "ubuntu-base-24-04": planned_base,
        },
    )

    command = build_command(target, builder=BuildBackend.DOCKER)

    assert (
        f"nexus.example.com/gitlab/ubuntu-base-24-04:latest=docker-image://{planned_base}"
    ) in command


def test_docker_buildx_uses_docker_image_contexts_and_loads_local_output() -> None:
    target = BuildPlanTarget(
        "app",
        ("selected",),
        ("base",),
        ("base",),
        "images/app/Containerfile",
        "images/app",
        "localhost/platform-images/app:dev",
        {"base": "localhost/platform-images/base:dev"},
        False,
    )

    assert build_command(target, builder=BuildBackend.DOCKER) == [
        "docker",
        "buildx",
        "build",
        "--load",
        "--build-context",
        "base=docker-image://localhost/platform-images/base:dev",
        "--file",
        "images/app/Containerfile",
        "--tag",
        "localhost/platform-images/app:dev",
        "images/app",
    ]


def test_docker_ci_build_pushes_with_buildx_and_reads_registry_digest(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    graph = build_graph(discover_targets(root), RepositoryConfig.load(root))
    runner = RecordingRunner()

    result = execute_ci_build(
        graph,
        "base",
        "registry/platform-images/base:ci-1-abc",
        {},
        root=root,
        environment={
            "CI_COMMIT_SHA": "abc",
            "CI_PROJECT_URL": "https://github.com/example/project",
        },
        runner=runner,  # type: ignore[arg-type]
        builder=BuildBackend.DOCKER,
        registry_transport=RegistryTransport.DOCKER,
    )

    assert runner.commands[0][:2] == ["docker", "pull"]
    build = runner.commands[1]
    assert build[:4] == ["docker", "buildx", "build", "--push"]
    assert "--metadata-file" in build
    assert result["digest"] == DOCKER_DIGEST
    assert result["immutable_reference"] == f"registry/platform-images/base@{DOCKER_DIGEST}"


def test_ci_build_replaces_the_exact_qualified_dependency_source(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    source = "nexus.example.com/gitlab/base:latest"
    root = repository_factory(
        {
            "base": "FROM alpine\n",
            "application": f"FROM {source}\n",
        }
    )
    graph = build_graph(discover_targets(root), RepositoryConfig.load(root))
    runner = RecordingRunner()
    planned_base = "registry.example/gitlab/base@sha256:" + "a" * 64

    execute_ci_build(
        graph,
        "application",
        "registry.example/platform-images/application:sha-abc",
        {"base": planned_base},
        root=root,
        environment={
            "CI_COMMIT_SHA": "abc",
            "CI_PROJECT_URL": "https://github.com/example/project",
        },
        runner=runner,  # type: ignore[arg-type]
        builder=BuildBackend.DOCKER,
        registry_transport=RegistryTransport.DOCKER,
    )

    build = runner.commands[1]
    assert f"{source}=docker-image://{planned_base}" in build
    assert f"base=docker-image://{planned_base}" in build


@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (BuildBackend.PODMAN, "container-image://registry/base@sha256:base"),
        (BuildBackend.BUILDAH, "container-image://registry/base@sha256:base"),
        (BuildBackend.NERDCTL, "docker-image://registry/base@sha256:base"),
    ],
)
def test_additional_builders_bind_exact_named_contexts(
    builder: BuildBackend, expected: str
) -> None:
    target = BuildPlanTarget(
        "app",
        ("selected",),
        ("base",),
        ("base",),
        "images/app/Containerfile",
        "images/app",
        "localhost/platform-images/app:dev",
        {"base": "registry/base@sha256:base"},
        False,
    )

    command = build_command(target, builder=builder)

    assert command[0] == builder.value
    assert f"base={expected}" in command


def test_nerdctl_local_plan_hands_parent_to_consumer_as_exact_oci_layout(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "app": "FROM base\n"})
    config = RepositoryConfig.load(root)
    graph = build_graph(discover_targets(root), config)
    plan = local_plan(graph, config, frozenset({"app"}))
    runner = RecordingRunner()

    commands = execute_local_plan(
        plan,
        root=root,
        runner=runner,  # type: ignore[arg-type]
        builder=BuildBackend.NERDCTL,
    )

    assert [command[:2] for command in runner.commands] == [
        ["nerdctl", "build"],
        ["nerdctl", "save"],
        ["nerdctl", "build"],
    ]
    consumer_context = next(
        argument for argument in runner.commands[-1] if argument.startswith("base=oci-layout://")
    )
    assert "@sha256:" not in consumer_context
    assert "nerdctl save" in commands[1]


def test_nerdctl_local_context_rejects_an_ambiguous_oci_layout(tmp_path: Path) -> None:
    manifests = [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + character * 64,
        }
        for character in ("a", "b")
    ]
    (tmp_path / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "manifests": manifests}),
        encoding="utf-8",
    )

    with pytest.raises(PlatformImagesError, match="exactly one image manifest"):
        _oci_layout_reference(tmp_path, "localhost/platform-images/base:dev")


def test_nerdctl_ci_build_pushes_through_buildkit_and_verifies_registry_digest(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    graph = build_graph(discover_targets(root), RepositoryConfig.load(root))
    runner = RecordingRunner()

    result = execute_ci_build(
        graph,
        "base",
        "registry/platform-images/base:ci-1-abc",
        {},
        root=root,
        environment={
            "CI_COMMIT_SHA": "abc",
            "CI_PROJECT_URL": "https://github.com/example/project",
        },
        runner=runner,  # type: ignore[arg-type]
        builder=BuildBackend.NERDCTL,
        registry_transport=RegistryTransport.NERDCTL,
    )

    build = runner.commands[1]
    assert build[:2] == ["nerdctl", "build"]
    assert "type=image,name=registry/platform-images/base:ci-1-abc,push=true" in build
    assert runner.commands[-2][:2] == ["nerdctl", "pull"]
    assert runner.commands[-1][:3] == ["nerdctl", "image", "inspect"]
    assert result["digest"] == EXISTING_DIGEST


def test_buildah_ci_build_uses_shared_storage_transport_digest_file(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    graph = build_graph(discover_targets(root), RepositoryConfig.load(root))
    runner = RecordingRunner()

    result = execute_ci_build(
        graph,
        "base",
        "registry/platform-images/base:ci-1-abc",
        {},
        root=root,
        environment={
            "CI_COMMIT_SHA": "abc",
            "CI_PROJECT_URL": "https://gitlab.example/project",
        },
        runner=runner,  # type: ignore[arg-type]
        builder=BuildBackend.BUILDAH,
        registry_transport=RegistryTransport.BUILDAH,
    )

    assert runner.commands[1][:2] == ["buildah", "build"]
    assert runner.commands[2][:3] == ["buildah", "push", "--digestfile"]
    assert result["digest"] == PODMAN_DIGEST


def test_ci_build_validates_inputs_pushes_and_reads_digest(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    config = RepositoryConfig.load(root)
    graph = build_graph(discover_targets(root), config)
    runner = RecordingRunner()
    output = "registry/platform-images/curl:ci-1-abc"

    result = execute_ci_build(
        graph,
        "curl",
        output,
        {"base": "registry/platform-images/base@sha256:base"},
        root=root,
        environment={
            "CI_COMMIT_SHA": "abc",
            "CI_PROJECT_URL": "https://gitlab.example/project",
            "SOURCE_DATE_EPOCH": "0",
        },
        runner=runner,  # type: ignore[arg-type]
    )

    assert len(runner.commands) == 3
    assert runner.commands[0] == ["podman", "pull", output]
    build = runner.commands[1]
    assert "base=container-image://registry/platform-images/base@sha256:base" in build
    assert "org.opencontainers.image.revision=abc" in build
    assert "org.opencontainers.image.source=https://gitlab.example/project" in build
    assert "org.opencontainers.image.created=1970-01-01T00:00:00Z" in build
    assert "io.platform-images.target=curl" in build
    assert (
        'io.platform-images.input-references={"base":"registry/platform-images/base@sha256:base"}'
        in build
    )
    assert runner.commands[2][:3] == ["podman", "push", "--digestfile"]
    assert result == {
        "schema_version": 1,
        "target": "curl",
        "commit_sha": "abc",
        "source": "https://gitlab.example/project",
        "reference": output,
        "digest": PODMAN_DIGEST,
        "immutable_reference": f"registry/platform-images/curl@{PODMAN_DIGEST}",
        "input_references": {
            "base": "registry/platform-images/base@sha256:base",
        },
    }


@pytest.mark.parametrize(
    ("inputs", "message"),
    [({}, "missing required dependency inputs: base"), ({"other": "ref"}, "missing")],
)
def test_ci_build_rejects_incorrect_dependency_inputs(
    repository_factory: Callable[[dict[str, str]], Path],
    inputs: dict[str, str],
    message: str,
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    config = RepositoryConfig.load(root)
    graph = build_graph(discover_targets(root), config)
    with pytest.raises(PlatformImagesError, match=message):
        execute_ci_build(graph, "curl", "registry/curl:tag", inputs, root=root)


def test_ci_build_does_not_claim_success_when_push_fails(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config = RepositoryConfig.load(root)
    graph = build_graph(discover_targets(root), config)
    with pytest.raises(ProcessError, match="push failed"):
        execute_ci_build(
            graph,
            "base",
            "registry/base:tag",
            {},
            root=root,
            environment={
                "CI_COMMIT_SHA": "abc",
                "CI_PROJECT_URL": "https://gitlab.example/project",
            },
            runner=RecordingRunner(fail_push=True),  # type: ignore[arg-type]
        )


def test_ci_build_retry_reuses_exact_existing_immutable_image(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    graph = build_graph(discover_targets(root), RepositoryConfig.load(root))
    input_refs = {"base": "registry/platform-images/base@sha256:base"}
    labels = {
        "org.opencontainers.image.revision": "abc",
        "org.opencontainers.image.source": "https://gitlab.example/project",
        "io.platform-images.target": "curl",
        "io.platform-images.input-references": json.dumps(input_refs, separators=(",", ":")),
    }
    runner = RecordingRunner(existing_labels=labels)

    result = execute_ci_build(
        graph,
        "curl",
        "registry/platform-images/curl:ci-1-abc",
        input_refs,
        root=root,
        environment={
            "CI_COMMIT_SHA": "abc",
            "CI_PROJECT_URL": "https://gitlab.example/project",
        },
        runner=runner,  # type: ignore[arg-type]
    )

    assert [command[:3] for command in runner.commands] == [
        ["podman", "pull", "registry/platform-images/curl:ci-1-abc"],
        ["podman", "image", "inspect"],
    ]
    assert result["digest"] == EXISTING_DIGEST


@pytest.mark.parametrize(
    ("builder", "transport", "inspect_prefix"),
    [
        (
            BuildBackend.BUILDAH,
            RegistryTransport.BUILDAH,
            ["buildah", "inspect", "--type"],
        ),
        (
            BuildBackend.NERDCTL,
            RegistryTransport.NERDCTL,
            ["nerdctl", "image", "inspect"],
        ),
    ],
)
def test_additional_backends_reuse_only_identity_matching_existing_output(
    repository_factory: Callable[[dict[str, str]], Path],
    builder: BuildBackend,
    transport: RegistryTransport,
    inspect_prefix: list[str],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    graph = build_graph(discover_targets(root), RepositoryConfig.load(root))
    labels = {
        "org.opencontainers.image.revision": "abc",
        "org.opencontainers.image.source": "https://example.com/project",
        "io.platform-images.target": "base",
        "io.platform-images.input-references": "{}",
    }
    runner = RecordingRunner(existing_labels=labels)

    result = execute_ci_build(
        graph,
        "base",
        "registry/platform-images/base:sha-abc",
        {},
        root=root,
        environment={
            "CI_COMMIT_SHA": "abc",
            "CI_PROJECT_URL": "https://example.com/project",
        },
        runner=runner,  # type: ignore[arg-type]
        builder=builder,
        registry_transport=transport,
    )

    assert runner.commands[1][: len(inspect_prefix)] == inspect_prefix
    assert len(runner.commands) == 2
    assert result["digest"] == EXISTING_DIGEST


def test_ci_build_rejects_existing_tag_with_different_graph_inputs(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    graph = build_graph(discover_targets(root), RepositoryConfig.load(root))
    runner = RecordingRunner(
        existing_labels={
            "org.opencontainers.image.revision": "abc",
            "org.opencontainers.image.source": "unknown",
            "io.platform-images.target": "curl",
            "io.platform-images.input-references": "{}",
        }
    )
    with pytest.raises(PlatformImagesError, match="immutable output tag collision"):
        execute_ci_build(
            graph,
            "curl",
            "registry/platform-images/curl:ci-1-abc",
            {"base": "registry/platform-images/base@sha256:base"},
            root=root,
            environment={
                "CI_COMMIT_SHA": "abc",
                "CI_PROJECT_URL": "unknown",
            },
            runner=runner,  # type: ignore[arg-type]
        )


def test_ci_build_requires_identity_variables(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    graph = build_graph(discover_targets(root), RepositoryConfig.load(root))
    with pytest.raises(PlatformImagesError, match="CI_COMMIT_SHA"):
        execute_ci_build(graph, "base", "registry/base:tag", {}, root=root, environment={})


def test_parse_input_references_rejects_duplicates_and_malformed() -> None:
    assert parse_input_references(["base=registry/base:tag"]) == {"base": "registry/base:tag"}
    with pytest.raises(PlatformImagesError, match="expected"):
        parse_input_references(["base"])
    with pytest.raises(PlatformImagesError, match="duplicate"):
        parse_input_references(["base=a", "base=b"])
