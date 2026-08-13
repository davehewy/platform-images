from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from platform_images.build import (
    build_command,
    execute_ci_build,
    parse_input_references,
)
from platform_images.config import RepositoryConfig
from platform_images.discovery import discover_targets
from platform_images.errors import PlatformImagesError, ProcessError
from platform_images.graph import build_graph
from platform_images.models import BuildPlanTarget
from platform_images.process import ProcessResult


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
        if command[:2] == ["podman", "pull"]:
            if self.existing_labels is None:
                raise ProcessError("image not found")
            return ProcessResult("", "", 0)
        if command[:3] == ["podman", "image", "inspect"]:
            return ProcessResult(
                json.dumps(
                    [
                        {
                            "Digest": "sha256:existing",
                            "Labels": dict(self.existing_labels or {}),
                        }
                    ]
                ),
                "",
                0,
            )
        if command[:2] == ["podman", "push"]:
            if self.fail_push:
                raise ProcessError("push failed")
            digest_path = Path(command[command.index("--digestfile") + 1])
            digest_path.write_text("sha256:deadbeef\n", encoding="utf-8")
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
        "target": "curl",
        "reference": output,
        "digest": "sha256:deadbeef",
        "immutable_reference": "registry/platform-images/curl@sha256:deadbeef",
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
    assert result["digest"] == "sha256:existing"


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
