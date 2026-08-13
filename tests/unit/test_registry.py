from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from platform_images.config import RepositoryConfig
from platform_images.errors import MissingStableImageError, ProcessError, RegistryError
from platform_images.process import ProcessResult
from platform_images.references import ReferencePolicy, immutable_reference
from platform_images.registry import (
    ECRRegistryClient,
    PodmanRegistryClient,
    StaticRegistryClient,
    login_to_ecr,
)


class RegistryRunner:
    def __init__(self, output: str = "", fail: bool = False) -> None:
        self.output = output
        self.fail = fail
        self.commands: list[list[str]] = []
        self.inputs: list[str | None] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture_output: bool = False,
        input_text: str | None = None,
    ) -> ProcessResult:
        del cwd, env, capture_output
        self.commands.append(list(arguments))
        self.inputs.append(input_text)
        if self.fail:
            raise ProcessError("failed")
        return ProcessResult(self.output, "", 0)


def test_reference_policy(repository_factory: Callable[[dict[str, str]], Path]) -> None:
    config = RepositoryConfig.load(repository_factory({"base": "FROM alpine\n"}))
    policy = ReferencePolicy(config, "registry.example.com/", "12")
    assert policy.local("base") == "localhost/platform-images/base:dev"
    assert policy.stable("base") == "registry.example.com/platform-images/base:main"
    assert immutable_reference(policy.stable("base"), "sha256:x") == (
        "registry.example.com/platform-images/base@sha256:x"
    )


def test_ecr_resolver_returns_immutable_digest(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = RepositoryConfig.load(repository_factory({"base": "FROM alpine\n"}))
    runner = RegistryRunner("sha256:abc\n")
    registry = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"
    client = ECRRegistryClient(config, registry, runner)  # type: ignore[arg-type]
    assert client.resolve_stable("base") == (f"{registry}/platform-images/base@sha256:abc")
    assert runner.commands[0][0:3] == ["aws", "ecr", "describe-images"]
    assert runner.commands[0][3:7] == [
        "--registry-id",
        "123456789012",
        "--region",
        "eu-west-2",
    ]
    assert "platform-images/base" in runner.commands[0]


def test_ecr_and_static_resolvers_fail_clearly(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = RepositoryConfig.load(repository_factory({"base": "FROM alpine\n"}))
    registry = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"
    with pytest.raises(MissingStableImageError):
        ECRRegistryClient(config, registry, RegistryRunner("None\n")).resolve_stable("base")  # type: ignore[arg-type]
    with pytest.raises(MissingStableImageError):
        StaticRegistryClient({}).resolve_stable("base")
    with pytest.raises(RegistryError, match="not pinned"):
        StaticRegistryClient({"base": f"{registry}/platform-images/base:main"}).resolve_stable(
            "base"
        )


def test_ecr_resolver_rejects_ambiguous_registry_and_surfaces_operational_failure(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = RepositoryConfig.load(repository_factory({"base": "FROM alpine\n"}))
    with pytest.raises(RegistryError, match="requires an AWS ECR registry hostname"):
        ECRRegistryClient(config, "registry.example.com").resolve_stable("base")
    with pytest.raises(RegistryError, match="eu-west-2"):
        ECRRegistryClient(
            config,
            "123456789012.dkr.ecr.eu-west-2.amazonaws.com",
            RegistryRunner(fail=True),  # type: ignore[arg-type]
        ).resolve_stable("base")


def test_ecr_missing_image_error_is_not_misreported_as_aws_outage(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = RepositoryConfig.load(repository_factory({"base": "FROM alpine\n"}))
    runner = RegistryRunner()
    runner.fail = True

    def missing(*args, **kwargs):
        del args, kwargs
        raise ProcessError("ImageNotFoundException")

    runner.run = missing  # type: ignore[method-assign]
    with pytest.raises(MissingStableImageError):
        ECRRegistryClient(
            config,
            "123456789012.dkr.ecr.eu-west-2.amazonaws.com",
            runner,  # type: ignore[arg-type]
        ).resolve_stable("base")


def test_promotion_is_pull_tag_push() -> None:
    runner = RegistryRunner()
    PodmanRegistryClient(Path.cwd(), runner).promote(  # type: ignore[arg-type]
        "registry/base:sha-a", "registry/base:main"
    )
    assert runner.commands == [
        ["podman", "pull", "registry/base:sha-a"],
        ["podman", "tag", "registry/base:sha-a", "registry/base:main"],
        ["podman", "push", "registry/base:main"],
    ]


def test_ecr_login_uses_registry_region_and_password_stdin() -> None:
    runner = RegistryRunner("secret-token\n")
    registry = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"

    login_to_ecr(registry, cwd=Path.cwd(), runner=runner)  # type: ignore[arg-type]

    assert runner.commands == [
        ["aws", "ecr", "get-login-password", "--region", "eu-west-2"],
        [
            "podman",
            "login",
            "--username",
            "AWS",
            "--password-stdin",
            registry,
        ],
    ]
    assert runner.inputs == [None, "secret-token\n"]
