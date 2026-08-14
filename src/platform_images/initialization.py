from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from platform_images.backends import default_transport, validate_execution_pair
from platform_images.config import NAMESPACE_RE, RepositoryConfig
from platform_images.discovery import inspect_targets
from platform_images.dockerfile import parse_dockerfile
from platform_images.errors import PlatformImagesError
from platform_images.image_identity import (
    build_image_identity_resolver,
    registry_relative_repository,
    repository_name,
)
from platform_images.models import BuildBackend, ReferenceKind, RegistryTransport

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
    image_repositories: tuple[tuple[str, str], ...]


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


def _infer_image_repositories(
    target_directories: tuple[Path, ...], namespace: str
) -> dict[str, str]:
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
                if reference.resolved is None:
                    continue
                repository = repository_name(reference.resolved)
                if "/" not in repository:
                    continue
                target_name = repository.rsplit("/", 1)[-1]
                if target_name in target_names:
                    candidates.setdefault(target_name, set()).add(
                        registry_relative_repository(repository)
                    )

    inferred: dict[str, str] = {}
    for target_name, repositories in sorted(candidates.items()):
        if len(repositories) > 1:
            raise PlatformImagesError(
                f"conflicting published repositories inferred for image target {target_name}: "
                + ", ".join(sorted(repositories))
                + "; rerun init with a consistent reference or configure [images] manually"
            )
        repository = next(iter(repositories))
        if repository != f"{namespace}/{target_name}":
            inferred[target_name] = repository
    return inferred


def _infer_short_external_images(
    target_directories: tuple[Path, ...],
    namespace: str,
    image_repositories: dict[str, str],
) -> tuple[str, ...]:
    target_names = frozenset(directory.name for directory in target_directories)
    identities = build_image_identity_resolver(
        target_names,
        namespace,
        repositories=image_repositories,
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
    image_repositories: dict[str, str],
    builder: BuildBackend,
    transport: RegistryTransport,
) -> str:
    global_inputs = (
        ".github/workflows/**",
        ".gitlab/**",
        ".gitlab-ci.yml",
        CONFIGURATION_NAME,
    )
    image_configuration = "".join(
        f"\n[images.{json.dumps(target)}]\nrepository = {json.dumps(repository)}\n"
        for target, repository in sorted(image_repositories.items())
    )
    return f"""\
[registry]
namespace = {json.dumps(namespace)}
registry_environment_variable = "PLATFORM_IMAGES_REGISTRY"
stable_tag = "main"
transport = {json.dumps(transport.value)}

[tags]
ci_prefix = "ci"
commit_prefix = "sha"

[build]
backend = {json.dumps(builder.value)}
{image_configuration}

[dockerfile]
allowed_short_external_images = {_toml_array(allowed_short_external_images)}

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
) -> InitializationResult:
    root = root.resolve()
    if not root.is_dir():
        raise PlatformImagesError(f"repository root does not exist or is not a directory: {root}")
    configuration_path = root / CONFIGURATION_NAME
    if configuration_path.exists():
        raise PlatformImagesError(
            f"configuration already exists; refusing to overwrite it: {configuration_path}"
        )

    configured_namespace = namespace or _default_namespace(root)
    if not NAMESPACE_RE.fullmatch(configured_namespace):
        raise PlatformImagesError(
            "namespace must contain lowercase letters or numbers separated by '.', '_', '/', or '-'"
        )
    selected_transport = transport or default_transport(builder)
    validate_execution_pair(builder, selected_transport)
    configured_roots = (
        _normalize_roots(root, discovery_roots) if discovery_roots else _infer_roots(root)
    )
    target_directories = _target_directories(root, configured_roots)
    image_repositories = _infer_image_repositories(target_directories, configured_namespace)
    external_images = _infer_short_external_images(
        target_directories,
        configured_namespace,
        image_repositories,
    )
    contents = _configuration_text(
        namespace=configured_namespace,
        discovery_roots=configured_roots,
        allowed_short_external_images=external_images,
        image_repositories=image_repositories,
        builder=builder,
        transport=selected_transport,
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
        tuple(sorted(image_repositories.items())),
    )
