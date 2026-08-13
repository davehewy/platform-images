from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from platform_images.errors import ConfigurationError

NAMESPACE_RE = re.compile(r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")


@dataclass(frozen=True)
class RegistryConfig:
    namespace: str
    registry_environment_variable: str
    stable_tag: str


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
class RepositoryConfig:
    root: Path
    registry: RegistryConfig
    tags: TagConfig
    changes: ChangeConfig
    dockerfile: DockerfileConfig

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
            registry = RegistryConfig(
                namespace=str(registry_data["namespace"]),
                registry_environment_variable=str(registry_data["registry_environment_variable"]),
                stable_tag=str(registry_data["stable_tag"]),
            )
            tags = TagConfig(
                ci_prefix=str(tag_data["ci_prefix"]),
                commit_prefix=str(tag_data["commit_prefix"]),
            )
            raw_global_inputs = change_data["global_inputs"]
            raw_external_images = dockerfile_data.get("allowed_short_external_images", ())
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
        except (KeyError, OSError, TypeError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
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
        return cls(
            root=root,
            registry=registry,
            tags=tags,
            changes=ChangeConfig(global_inputs),
            dockerfile=DockerfileConfig(allowed_short_external_images),
        )
