from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from platform_images.errors import ConfigurationError, PlatformImagesError
from platform_images.models import BuildBackend, RegistryTransport


class PushStrategy(StrEnum):
    BUILDX_METADATA = "buildx-metadata"
    DIRECT_INSPECT = "direct-inspect"
    TRANSPORT_DIGEST_FILE = "transport-digest-file"


@dataclass(frozen=True)
class BuilderCapabilities:
    executable: str
    context_scheme: str
    push_strategy: PushStrategy
    storage_family: str
    minimum_version: str


@dataclass(frozen=True)
class TransportCapabilities:
    executable: str
    digest_file: bool
    storage_family: str
    minimum_version: str


BUILDERS: dict[BuildBackend, BuilderCapabilities] = {
    BuildBackend.DOCKER: BuilderCapabilities(
        "docker", "docker-image://", PushStrategy.BUILDX_METADATA, "docker", "0.12"
    ),
    BuildBackend.PODMAN: BuilderCapabilities(
        "podman", "container-image://", PushStrategy.TRANSPORT_DIGEST_FILE, "containers", "4.4"
    ),
    BuildBackend.BUILDAH: BuilderCapabilities(
        "buildah", "container-image://", PushStrategy.TRANSPORT_DIGEST_FILE, "containers", "1.33"
    ),
    BuildBackend.NERDCTL: BuilderCapabilities(
        "nerdctl", "docker-image://", PushStrategy.DIRECT_INSPECT, "containerd", "2.0"
    ),
}

TRANSPORTS: dict[RegistryTransport, TransportCapabilities] = {
    RegistryTransport.DOCKER: TransportCapabilities("docker", False, "docker", "24.0"),
    RegistryTransport.PODMAN: TransportCapabilities("podman", True, "containers", "4.4"),
    RegistryTransport.BUILDAH: TransportCapabilities("buildah", True, "containers", "1.33"),
    RegistryTransport.NERDCTL: TransportCapabilities("nerdctl", False, "containerd", "2.0"),
}


def default_transport(builder: BuildBackend) -> RegistryTransport:
    return RegistryTransport(builder.value)


def validate_execution_pair(
    builder: BuildBackend,
    transport: RegistryTransport,
    *,
    configuration: bool = False,
) -> None:
    builder_capabilities = BUILDERS[builder]
    transport_capabilities = TRANSPORTS[transport]
    if builder_capabilities.storage_family == transport_capabilities.storage_family:
        return
    error = ConfigurationError if configuration else PlatformImagesError
    raise error(
        f"build backend {builder.value!r} cannot use registry transport {transport.value!r}; "
        "Docker and nerdctl require their matching transport, while Podman and Buildah may share "
        "either containers-storage transport"
    )


def inspect_command(transport: RegistryTransport, reference: str) -> list[str]:
    executable = TRANSPORTS[transport].executable
    if transport is RegistryTransport.BUILDAH:
        return [executable, "inspect", "--type", "image", reference]
    return [executable, "image", "inspect", reference]


def push_command(
    transport: RegistryTransport,
    reference: str,
    *,
    digest_file: str | None = None,
) -> list[str]:
    capabilities = TRANSPORTS[transport]
    command = [capabilities.executable, "push"]
    if digest_file is not None:
        if not capabilities.digest_file:
            raise PlatformImagesError(
                f"registry transport {transport.value!r} cannot write an exact push digest file"
            )
        command.extend(["--digestfile", digest_file])
    command.append(reference)
    return command
