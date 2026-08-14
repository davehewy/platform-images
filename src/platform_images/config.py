from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platform_images.backends import default_transport, validate_execution_pair
from platform_images.errors import ConfigurationError
from platform_images.image_identity import registry_relative_repository, repository_name
from platform_images.models import BuildBackend, RegistryTransport

NAMESPACE_RE = re.compile(r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")
TARGET_NAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
IMAGE_ALIAS_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$"
)


@dataclass(frozen=True)
class RegistryConfig:
    namespace: str
    registry_environment_variable: str
    stable_tag: str
    transport: RegistryTransport


@dataclass(frozen=True)
class TagConfig:
    ci_prefix: str
    commit_prefix: str


@dataclass(frozen=True)
class ChangeConfig:
    global_inputs: tuple[str, ...]


@dataclass(frozen=True)
class DockerfileConfig:
    allowed_short_external_images: frozenset[str]


@dataclass(frozen=True)
class ImageConfig:
    repository: str | None
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryConfig:
    roots: tuple[str, ...]

    @property
    def root(self) -> str:
        """Backward-compatible access for configurations with one discovery root."""
        return self.roots[0]


@dataclass(frozen=True)
class BuildConfig:
    backend: BuildBackend

    @property
    def engine(self) -> BuildBackend:
        """Backward-compatible name for the pre-v0.5 combined execution setting."""
        return self.backend


@dataclass(frozen=True)
class RepositoryConfig:
    root: Path
    registry: RegistryConfig
    tags: TagConfig
    changes: ChangeConfig
    dockerfile: DockerfileConfig
    images: Mapping[str, ImageConfig]
    discovery: DiscoveryConfig
    build: BuildConfig

    def image_repository(self, target: str) -> str:
        configured = self.images.get(target)
        if configured is not None and configured.repository is not None:
            return configured.repository
        return f"{self.registry.namespace}/{target}"

    def image_aliases(self, target: str) -> tuple[str, ...]:
        configured = self.images.get(target)
        return configured.aliases if configured is not None else ()

    @classmethod
    def load(cls, root: Path) -> RepositoryConfig:
        root = root.resolve()
        path = root / "platform-images.toml"
        if not path.is_file():
            raise ConfigurationError(f"configuration file does not exist: {path}")
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise ConfigurationError(
                f"configuration file resolves outside the repository: {path}"
            ) from exc
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            registry_data = data["registry"]
            tag_data = data["tags"]
            change_data = data["changes"]
            dockerfile_data = data.get("dockerfile", {})
            image_data = data.get("images", {})
            discovery_data = data.get("discovery", {})
            build_data = data.get("build", {})
            tags = TagConfig(
                ci_prefix=str(tag_data["ci_prefix"]),
                commit_prefix=str(tag_data["commit_prefix"]),
            )
            raw_global_inputs = change_data["global_inputs"]
            raw_external_images = dockerfile_data.get("allowed_short_external_images", ())
            configured_root = discovery_data.get("root")
            configured_roots = discovery_data.get("roots")
            if configured_root is not None and configured_roots is not None:
                raise TypeError("discovery.root and discovery.roots cannot both be set")
            if configured_roots is None:
                raw_discovery_roots = ["images" if configured_root is None else configured_root]
            else:
                raw_discovery_roots = configured_roots
            if (
                not isinstance(raw_discovery_roots, list)
                or not raw_discovery_roots
                or not all(isinstance(item, str) for item in raw_discovery_roots)
            ):
                raise TypeError("discovery.roots must be a non-empty array of strings")
            legacy_engine = build_data.get("engine")
            configured_backend = build_data.get("backend")
            if legacy_engine is not None and configured_backend is not None:
                raise TypeError("build.engine and build.backend cannot both be set")
            backend = BuildBackend(
                str(
                    configured_backend
                    if configured_backend is not None
                    else legacy_engine
                    if legacy_engine is not None
                    else BuildBackend.PODMAN.value
                )
            )
            transport = RegistryTransport(
                str(registry_data.get("transport", default_transport(backend).value))
            )
            registry = RegistryConfig(
                namespace=str(registry_data["namespace"]),
                registry_environment_variable=str(registry_data["registry_environment_variable"]),
                stable_tag=str(registry_data["stable_tag"]),
                transport=transport,
            )
            validate_execution_pair(backend, transport, configuration=True)
            if not isinstance(raw_global_inputs, list) or not all(
                isinstance(item, str) for item in raw_global_inputs
            ):
                raise TypeError("changes.global_inputs must be an array of strings")
            if not isinstance(raw_external_images, list | tuple) or not all(
                isinstance(item, str) for item in raw_external_images
            ):
                raise TypeError(
                    "dockerfile.allowed_short_external_images must be an array of strings"
                )
            global_inputs = tuple(sorted(raw_global_inputs))
            allowed_short_external_images = frozenset(raw_external_images) | {"scratch"}
            if not isinstance(image_data, dict):
                raise TypeError("images must be a table keyed by logical target name")
            images: dict[str, ImageConfig] = {}
            for target_name, raw_image in image_data.items():
                if not isinstance(target_name, str) or not isinstance(raw_image, dict):
                    raise TypeError("each images entry must be a table")
                unexpected = sorted(set(raw_image) - {"repository", "aliases"})
                if unexpected:
                    raise TypeError(
                        f"images.{target_name} contains unknown settings: " + ", ".join(unexpected)
                    )
                raw_repository = raw_image.get("repository")
                raw_aliases = raw_image.get("aliases", [])
                if raw_repository is not None and not isinstance(raw_repository, str):
                    raise TypeError(f"images.{target_name}.repository must be a string")
                if not isinstance(raw_aliases, list) or not all(
                    isinstance(alias, str) for alias in raw_aliases
                ):
                    raise TypeError(f"images.{target_name}.aliases must be an array of strings")
                images[target_name] = ImageConfig(
                    repository=raw_repository,
                    aliases=tuple(sorted(raw_aliases)),
                )
        except (
            KeyError,
            OSError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            tomllib.TOMLDecodeError,
        ) as exc:
            raise ConfigurationError(f"invalid configuration in {path}: {exc}") from exc
        if not registry.registry_environment_variable.isidentifier():
            raise ConfigurationError(
                "registry_environment_variable must be a valid environment variable name"
            )
        invalid_external_names = sorted(
            name
            for name in allowed_short_external_images
            if not name or "/" in name or ":" in name or "@" in name
        )
        if invalid_external_names:
            raise ConfigurationError(
                "allowed_short_external_images entries must be unqualified repository names: "
                + ", ".join(invalid_external_names)
            )
        for target_name, image in images.items():
            if not TARGET_NAME_RE.fullmatch(target_name):
                raise ConfigurationError(f"invalid logical image target name: {target_name}")
            if image.repository is not None and (
                not NAMESPACE_RE.fullmatch(image.repository)
                or registry_relative_repository(image.repository) != image.repository
            ):
                raise ConfigurationError(
                    f"images.{target_name}.repository must be a lowercase registry-relative "
                    "repository path without a tag or digest"
                )
            invalid_aliases = sorted(
                alias
                for alias in image.aliases
                if not alias
                or alias != alias.casefold()
                or "://" in alias
                or "@" in alias
                or any(character.isspace() for character in alias)
                or repository_name(alias) != alias
                or not IMAGE_ALIAS_RE.fullmatch(alias)
            )
            if invalid_aliases:
                raise ConfigurationError(
                    f"images.{target_name}.aliases must contain lowercase image repositories "
                    "without tags or digests: " + ", ".join(invalid_aliases)
                )
            if len(image.aliases) != len(set(image.aliases)):
                raise ConfigurationError(f"images.{target_name}.aliases must be unique")
        discovery_paths: list[Path] = []
        for raw_discovery_root in raw_discovery_roots:
            discovery_path = Path(raw_discovery_root)
            if (
                not raw_discovery_root
                or discovery_path.is_absolute()
                or discovery_path in {Path("."), Path("..")}
                or ".." in discovery_path.parts
            ):
                raise ConfigurationError(
                    "discovery roots must be non-empty repository-relative directories"
                )
            try:
                (root / discovery_path).resolve().relative_to(root)
            except ValueError as exc:
                raise ConfigurationError(
                    f"discovery root resolves outside the repository: {raw_discovery_root}"
                ) from exc
            discovery_paths.append(discovery_path)

        normalized_roots = tuple(path.as_posix() for path in discovery_paths)
        folded_roots = [value.casefold() for value in normalized_roots]
        if len(folded_roots) != len(set(folded_roots)):
            raise ConfigurationError("discovery roots must be unique")
        return cls(
            root=root,
            registry=registry,
            tags=tags,
            changes=ChangeConfig(global_inputs),
            dockerfile=DockerfileConfig(allowed_short_external_images),
            images=dict(sorted(images.items())),
            discovery=DiscoveryConfig(normalized_roots),
            build=BuildConfig(backend),
        )
