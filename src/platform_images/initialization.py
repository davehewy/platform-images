from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from platform_images.backends import default_transport, validate_execution_pair
from platform_images.config import NAMESPACE_RE, VARIABLE_NAME_RE, RepositoryConfig
from platform_images.discovery import inspect_targets
from platform_images.dockerfile import parse_dockerfile
from platform_images.errors import PlatformImagesError
from platform_images.image_identity import (
    build_image_identity_resolver,
    registry_relative_repository,
    repository_name,
)
from platform_images.models import (
    BuildBackend,
    ReferenceKind,
    RegistryAuthentication,
    RegistryProvider,
    RegistryTransport,
)

CONFIGURATION_NAME = "platform-images.toml"
_IGNORED_DISCOVERY_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)


@dataclass(frozen=True)
class InitializationResult:
    configuration_path: Path
    discovery_roots: tuple[str, ...]
    target_count: int
    allowed_short_external_images: tuple[str, ...]
    inferred_registry_namespace: str | None
    source_repository_namespaces: tuple[str, ...]


def _default_namespace(root: Path) -> str:
    parts = re.findall(r"[a-z0-9]+", root.name.casefold())
    return "-".join(parts) or "container-images"


def _find_build_files(root: Path) -> tuple[Path, ...]:
    build_files: list[Path] = []
    for directory, child_directories, file_names in os.walk(root, followlinks=False):
        child_directories[:] = sorted(
            name
            for name in child_directories
            if name not in _IGNORED_DISCOVERY_DIRECTORIES and not name.startswith(".")
        )
        current = Path(directory)
        current_files = [
            current / name for name in ("Dockerfile", "Containerfile") if name in file_names
        ]
        if current_files:
            build_files.extend(current_files)
            # A target's context is opaque to discovery. Do not mistake vendored or example
            # Dockerfiles inside that context for additional repository image targets.
            child_directories[:] = []
    return tuple(sorted(build_files))


def _normalize_roots(root: Path, values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        path = Path(value)
        if not value or path.is_absolute() or path in {Path("."), Path("..")} or ".." in path.parts:
            raise PlatformImagesError(
                "discovery roots must be repository-relative directories other than '.'"
            )
        try:
            resolved = (root / path).resolve()
            resolved.relative_to(root)
        except ValueError as exc:
            raise PlatformImagesError(
                f"discovery root resolves outside the repository: {value}"
            ) from exc
        if not resolved.is_dir():
            raise PlatformImagesError(
                f"discovery root does not exist or is not a directory: {value}"
            )
        normalized.append(resolved.relative_to(root).as_posix())

    ordered = tuple(sorted(normalized))
    folded = [value.casefold() for value in ordered]
    if len(folded) != len(set(folded)):
        raise PlatformImagesError("discovery roots must be unique")
    return ordered


def _infer_roots(root: Path) -> tuple[str, ...]:
    build_files = _find_build_files(root)
    if not build_files:
        raise PlatformImagesError(
            "no Dockerfile or Containerfile target groups could be inferred; "
            "pass --discovery-root PATH for each existing target parent directory"
        )
    if any(path.parent == root for path in build_files):
        raise PlatformImagesError(
            "a repository-root Dockerfile or Containerfile cannot be represented as a named "
            "image target; place it in a target directory beneath a discovery root"
        )
    candidates = tuple(
        sorted({path.parent.parent.relative_to(root).as_posix() for path in build_files})
    )
    return _normalize_roots(root, candidates)


def _target_directories(root: Path, discovery_roots: tuple[str, ...]) -> tuple[Path, ...]:
    directories: list[Path] = []
    for target in inspect_targets(root, discovery_roots).entries:
        try:
            target.directory.resolve().relative_to(root)
        except ValueError as exc:
            raise PlatformImagesError(
                f"image target directory resolves outside the repository: {target.directory}"
            ) from exc
        directories.append(target.directory)
    return tuple(directories)


def _find_qualified_local_repositories(
    target_directories: tuple[Path, ...], namespace: str
) -> dict[str, frozenset[str]]:
    target_names = frozenset(directory.name for directory in target_directories)
    candidates: dict[str, set[str]] = {}
    for directory in target_directories:
        for build_file in (directory / "Dockerfile", directory / "Containerfile"):
            if not build_file.is_file():
                continue
            try:
                text = build_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise PlatformImagesError(f"unable to inspect {build_file}: {exc}") from exc
            parsed = parse_dockerfile(
                text,
                target_names=target_names,
                internal_namespace=namespace,
            )
            for reference in parsed.references:
                source = reference.source or reference.resolved
                if source is None:
                    continue
                repository = repository_name(source)
                if "/" not in repository:
                    continue
                target_name = repository.rsplit("/", 1)[-1]
                if target_name in target_names:
                    candidates.setdefault(target_name, set()).add(
                        registry_relative_repository(repository)
                    )

    return {target: frozenset(repositories) for target, repositories in sorted(candidates.items())}


def _repository_namespaces(
    repositories: dict[str, frozenset[str]],
) -> tuple[str, ...]:
    observed = {repository for values in repositories.values() for repository in values}
    if any("/" not in repository for repository in observed):
        return ()
    return tuple(sorted({repository.rsplit("/", 1)[0] for repository in observed}))


def _common_repository_namespace(repositories: dict[str, frozenset[str]]) -> str | None:
    if not repositories:
        return None
    namespaces = _repository_namespaces(repositories)
    return namespaces[0] if len(namespaces) == 1 else None


def _infer_short_external_images(
    target_directories: tuple[Path, ...],
    namespace: str,
) -> tuple[str, ...]:
    target_names = frozenset(directory.name for directory in target_directories)
    identities = build_image_identity_resolver(
        target_names,
        namespace,
    )
    external_images: set[str] = set()
    for directory in target_directories:
        build_files = [
            path
            for path in (directory / "Dockerfile", directory / "Containerfile")
            if path.is_file()
        ]
        for build_file in build_files:
            try:
                text = build_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise PlatformImagesError(f"unable to inspect {build_file}: {exc}") from exc
            parsed = parse_dockerfile(
                text,
                target_names=target_names,
                internal_namespace=namespace,
                image_identities=identities,
            )
            for reference in parsed.references:
                if reference.kind is not ReferenceKind.UNRESOLVED or reference.resolved is None:
                    continue
                repository = repository_name(reference.resolved)
                resembles_local_target = difflib.get_close_matches(
                    repository, target_names, n=1, cutoff=0.75
                )
                if (
                    "/" not in repository
                    and repository not in target_names
                    and not resembles_local_target
                ):
                    external_images.add(repository)
    return tuple(sorted(external_images))


def _toml_array(values: tuple[str, ...]) -> str:
    if not values:
        return "[]"
    items = "\n".join(f"  {json.dumps(value)}," for value in values)
    return f"[\n{items}\n]"


def _configuration_text(
    *,
    namespace: str,
    discovery_roots: tuple[str, ...],
    allowed_short_external_images: tuple[str, ...],
    builder: BuildBackend,
    transport: RegistryTransport,
    registry_provider: RegistryProvider,
    registry_authentication: RegistryAuthentication,
    registry_username_environment_variable: str,
    registry_password_environment_variable: str,
    build_arguments: dict[str, str],
) -> str:
    global_inputs = (
        ".github/workflows/**",
        ".gitlab/**",
        ".gitlab-ci.yml",
        CONFIGURATION_NAME,
    )
    rendered_arguments = "\n".join(
        f"{json.dumps(name)} = {json.dumps(value)}"
        for name, value in sorted(build_arguments.items())
    )
    arguments_configuration = (
        f"\n[dockerfile.arguments]\n{rendered_arguments}\n" if rendered_arguments else ""
    )
    return f"""\
[registry]
namespace = {json.dumps(namespace)}
registry_environment_variable = "PLATFORM_IMAGES_REGISTRY"
stable_tag = "main"
transport = {json.dumps(transport.value)}
provider = {json.dumps(registry_provider.value)}
authentication = {json.dumps(registry_authentication.value)}
username_environment_variable = {json.dumps(registry_username_environment_variable)}
password_environment_variable = {json.dumps(registry_password_environment_variable)}
scheme = "https"

[tags]
ci_prefix = "ci"
commit_prefix = "sha"

[build]
backend = {json.dumps(builder.value)}

[dockerfile]
allowed_short_external_images = {_toml_array(allowed_short_external_images)}
{arguments_configuration}

[changes]
global_inputs = {_toml_array(global_inputs)}

[discovery]
roots = {_toml_array(discovery_roots)}
"""


def initialize_repository(
    root: Path,
    *,
    discovery_roots: tuple[str, ...] = (),
    namespace: str | None = None,
    builder: BuildBackend = BuildBackend.DOCKER,
    transport: RegistryTransport | None = None,
    registry_provider: RegistryProvider = RegistryProvider.OCI,
    registry_authentication: RegistryAuthentication | None = None,
    registry_username_environment_variable: str = "PLATFORM_IMAGES_REGISTRY_USERNAME",
    registry_password_environment_variable: str = "PLATFORM_IMAGES_REGISTRY_PASSWORD",
    build_arguments: dict[str, str] | None = None,
) -> InitializationResult:
    root = root.resolve()
    if not root.is_dir():
        raise PlatformImagesError(f"repository root does not exist or is not a directory: {root}")
    configuration_path = root / CONFIGURATION_NAME
    if configuration_path.exists():
        raise PlatformImagesError(
            f"configuration already exists; refusing to overwrite it: {configuration_path}"
        )

    default_namespace = _default_namespace(root)
    requested_namespace = namespace or default_namespace
    if not NAMESPACE_RE.fullmatch(requested_namespace):
        raise PlatformImagesError(
            "namespace must contain lowercase letters or numbers separated by '.', '_', '/', or '-'"
        )
    selected_transport = transport or default_transport(builder)
    validate_execution_pair(builder, selected_transport)
    selected_authentication = registry_authentication or (
        RegistryAuthentication.ECR
        if registry_provider is RegistryProvider.ECR
        else RegistryAuthentication.CREDENTIALS
    )
    if registry_provider is RegistryProvider.ECR and (
        selected_authentication is not RegistryAuthentication.ECR
    ):
        raise PlatformImagesError("the ECR provider requires --registry-authentication ecr")
    if registry_provider is RegistryProvider.OCI and (
        selected_authentication is RegistryAuthentication.ECR
    ):
        raise PlatformImagesError(
            "the OCI provider requires --registry-authentication credentials or ambient"
        )
    for value, option in (
        (registry_username_environment_variable, "--registry-username-variable"),
        (registry_password_environment_variable, "--registry-password-variable"),
    ):
        if VARIABLE_NAME_RE.fullmatch(value) is None:
            raise PlatformImagesError(f"{option} must be a valid environment variable name")
    selected_build_arguments = build_arguments or {}
    invalid_build_arguments = sorted(
        name
        for name, value in selected_build_arguments.items()
        if VARIABLE_NAME_RE.fullmatch(name) is None or "\x00" in value
    )
    if invalid_build_arguments:
        raise PlatformImagesError(
            "--build-arg names must be valid Dockerfile ARG names and values must not contain "
            "NUL bytes: " + ", ".join(invalid_build_arguments)
        )
    configured_roots = (
        _normalize_roots(root, discovery_roots) if discovery_roots else _infer_roots(root)
    )
    target_directories = _target_directories(root, configured_roots)
    observed_repositories = _find_qualified_local_repositories(
        target_directories, requested_namespace
    )
    inferred_namespace = (
        _common_repository_namespace(observed_repositories) if namespace is None else None
    )
    configured_namespace = inferred_namespace or requested_namespace
    external_images = _infer_short_external_images(
        target_directories,
        configured_namespace,
    )
    contents = _configuration_text(
        namespace=configured_namespace,
        discovery_roots=configured_roots,
        allowed_short_external_images=external_images,
        builder=builder,
        transport=selected_transport,
        registry_provider=registry_provider,
        registry_authentication=selected_authentication,
        registry_username_environment_variable=registry_username_environment_variable,
        registry_password_environment_variable=registry_password_environment_variable,
        build_arguments=selected_build_arguments,
    )

    try:
        with configuration_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
        RepositoryConfig.load(root)
    except FileExistsError as exc:
        raise PlatformImagesError(
            f"configuration already exists; refusing to overwrite it: {configuration_path}"
        ) from exc
    except Exception:
        configuration_path.unlink(missing_ok=True)
        raise

    return InitializationResult(
        configuration_path,
        configured_roots,
        len(target_directories),
        external_images,
        inferred_namespace,
        _repository_namespaces(observed_repositories),
    )
