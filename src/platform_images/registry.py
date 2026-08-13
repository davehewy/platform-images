from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from platform_images.backends import TRANSPORTS
from platform_images.config import RepositoryConfig
from platform_images.errors import MissingStableImageError, ProcessError, RegistryError
from platform_images.models import BuildBackend, RegistryTransport
from platform_images.process import ProcessRunner
from platform_images.references import ReferencePolicy, immutable_reference

ECR_REGISTRY_RE = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr(?:-fips)?\."
    r"(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$"
)


def ecr_registry_coordinates(registry: str) -> tuple[str, str, str]:
    normalized = registry.rstrip("/")
    match = ECR_REGISTRY_RE.fullmatch(normalized)
    if not match:
        raise RegistryError(
            "AWS ECR operation requires an ECR registry hostname of the form "
            "<12-digit-account>.dkr.ecr.<region>.amazonaws.com"
        )
    return normalized, match.group("account"), match.group("region")


class RegistryClient:
    def resolve_stable(self, target: str) -> str:
        raise NotImplementedError

    def promote(self, source: str, destination: str) -> None:
        raise NotImplementedError


class StaticRegistryClient(RegistryClient):
    def __init__(self, references: Mapping[str, str]) -> None:
        self.references = dict(references)

    def resolve_stable(self, target: str) -> str:
        try:
            reference = self.references[target]
        except KeyError as exc:
            raise MissingStableImageError(target) from exc
        if "@sha256:" not in reference:
            raise RegistryError(
                f'static stable reference for "{target}" is not pinned by sha256 digest'
            )
        return reference

    def promote(self, source: str, destination: str) -> None:
        del source, destination
        raise NotImplementedError("static reference clients cannot promote images")


class ECRRegistryClient(RegistryClient):
    def __init__(
        self,
        config: RepositoryConfig,
        registry: str,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.config = config
        self.registry = registry.rstrip("/")
        self.runner = runner or ProcessRunner()
        match = ECR_REGISTRY_RE.fullmatch(self.registry)
        self.registry_id = match.group("account") if match else None
        self.region = match.group("region") if match else None

    def resolve_stable(self, target: str) -> str:
        if self.registry_id is None or self.region is None:
            raise RegistryError(
                "automatic stable-image resolution requires an AWS ECR registry hostname; "
                "set PLATFORM_IMAGES_STABLE_REFS for another registry"
            )
        repository = f"{self.config.registry.namespace}/{target}"
        try:
            result = self.runner.run(
                [
                    "aws",
                    "ecr",
                    "describe-images",
                    "--registry-id",
                    self.registry_id,
                    "--region",
                    self.region,
                    "--repository-name",
                    repository,
                    "--image-ids",
                    f"imageTag={self.config.registry.stable_tag}",
                    "--query",
                    "imageDetails[0].imageDigest",
                    "--output",
                    "text",
                ],
                cwd=self.config.root,
                capture_output=True,
            )
        except ProcessError as exc:
            if "ImageNotFoundException" in str(exc):
                raise MissingStableImageError(target) from exc
            raise RegistryError(
                f'unable to resolve stable image for "{target}" in '
                f"AWS account {self.registry_id}, region {self.region}: {exc}"
            ) from exc
        digest = result.stdout.strip()
        if not digest.startswith("sha256:"):
            raise MissingStableImageError(target)
        stable = ReferencePolicy(self.config, self.registry).stable(target)
        return immutable_reference(stable, digest)

    def promote(self, source: str, destination: str) -> None:
        _container_promote(
            source,
            destination,
            transport=self.config.registry.transport,
            runner=self.runner,
            cwd=self.config.root,
        )


class ContainerRegistryClient(RegistryClient):
    """Registry transport used by the promotion command."""

    def __init__(
        self,
        cwd: Path,
        runner: ProcessRunner | None = None,
        transport: RegistryTransport = RegistryTransport.PODMAN,
    ) -> None:
        self.cwd = cwd
        self.runner = runner or ProcessRunner()
        self.transport = RegistryTransport(transport)

    def resolve_stable(self, target: str) -> str:
        del target
        raise NotImplementedError("container transports do not resolve stable ECR tags")

    def promote(self, source: str, destination: str) -> None:
        _container_promote(
            source,
            destination,
            transport=self.transport,
            runner=self.runner,
            cwd=self.cwd,
        )


class PodmanRegistryClient(ContainerRegistryClient):
    """Backward-compatible Podman registry transport."""


def registry_from_environment_json(value: str | None) -> RegistryClient | None:
    if value is None:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in parsed.items()
    ):
        raise ValueError("PLATFORM_IMAGES_STABLE_REFS must be a JSON object of strings")
    return StaticRegistryClient(parsed)


def login_to_ecr(
    registry: str,
    *,
    cwd: Path,
    runner: ProcessRunner | None = None,
    transport: RegistryTransport | None = None,
    engine: BuildBackend | None = None,
) -> None:
    if transport is not None and engine is not None and transport.value != engine.value:
        raise ValueError("transport and legacy engine select different executables")
    transport = transport or RegistryTransport(engine.value if engine else "podman")
    normalized, _account, region = ecr_registry_coordinates(registry)
    process = runner or ProcessRunner()
    password = process.run(
        ["aws", "ecr", "get-login-password", "--region", region],
        cwd=cwd,
        capture_output=True,
    ).stdout
    if not password.strip():
        raise RegistryError("AWS returned an empty ECR login password")
    process.run(
        [
            TRANSPORTS[transport].executable,
            "login",
            "--username",
            "AWS",
            "--password-stdin",
            normalized,
        ],
        cwd=cwd,
        capture_output=True,
        input_text=password,
    )


def _container_promote(
    source: str,
    destination: str,
    *,
    runner: ProcessRunner | None = None,
    cwd: Path | None = None,
    transport: RegistryTransport = RegistryTransport.PODMAN,
) -> None:
    process = runner or ProcessRunner()
    executable = TRANSPORTS[transport].executable
    process.run([executable, "pull", source], cwd=cwd)
    process.run([executable, "tag", source, destination], cwd=cwd)
    process.run([executable, "push", destination], cwd=cwd)
