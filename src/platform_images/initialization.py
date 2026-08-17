from __future__ import annotations

import difflib
import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platform_images.backends import default_transport, validate_execution_pair
from platform_images.config import NAMESPACE_RE, VARIABLE_NAME_RE, ImageConfig, RepositoryConfig
from platform_images.discovery import inspect_targets
from platform_images.dockerfile import parse_dockerfile
from platform_images.errors import PlatformImagesError
from platform_images.image_identity import (
    build_image_identity_resolver,
    compact_image_name,
    probable_target_matches,
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
_STRONG_MATCH_MINIMUM = 0.84
_STRONG_MATCH_MARGIN = 0.08
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
    repository_inferences: tuple[RepositoryInference, ...]


@dataclass(frozen=True)
class RepositoryInference:
    target: str
    source_repository: str
    action: str
    confidence: str
    reference_count: int


@dataclass(frozen=True)
class _RepositoryObservation:
    target: str
    repository: str
    confidence: str
    score: float
    reference_count: int


@dataclass(frozen=True)
class _RepositoryPolicy:
    namespace: str | None
    source_namespaces: tuple[str, ...]
    images: Mapping[str, ImageConfig]
    review: tuple[RepositoryInference, ...]


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


def _qualified_repository_reference_counts(
    target_directories: tuple[Path, ...], namespace: str
) -> Counter[str]:
    target_names = frozenset(directory.name for directory in target_directories)
    references: Counter[str] = Counter()
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
                references[registry_relative_repository(repository)] += 1

    return references


def _infer_repository_target(
    repository: str,
    target_names: frozenset[str],
) -> tuple[str, str, float] | None:
    basename = repository.rsplit("/", 1)[-1]
    if basename in target_names:
        return basename, "exact", 1.0

    compact_basename = compact_image_name(basename)
    normalized = sorted(
        target for target in target_names if compact_image_name(target) == compact_basename
    )
    if len(normalized) == 1:
        return normalized[0], "separator-normalized", 1.0
    if normalized:
        # A punctuation-only collision is genuinely ambiguous. Do not let fuzzy scoring pick one.
        return None

    ranked = probable_target_matches(repository, target_names)
    if not ranked:
        return None
    target, score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if score >= _STRONG_MATCH_MINIMUM and score - runner_up >= _STRONG_MATCH_MARGIN:
        return target, "strong-similarity", score
    return None


def _repository_namespace(repository: str) -> str | None:
    return repository.rsplit("/", 1)[0] if "/" in repository else None


def _select_repository_namespace(
    observations: tuple[_RepositoryObservation, ...],
) -> str | None:
    by_namespace: dict[str, list[_RepositoryObservation]] = defaultdict(list)
    for observation in observations:
        namespace = _repository_namespace(observation.repository)
        if namespace is not None:
            by_namespace[namespace].append(observation)
    if not by_namespace:
        return None

    def rank(values: list[_RepositoryObservation]) -> tuple[int, int, int]:
        targets = {value.target for value in values}
        references = sum(value.reference_count for value in values)
        confidence = sum(
            {"exact": 3, "separator-normalized": 2, "strong-similarity": 1}[value.confidence]
            for value in values
        )
        return len(targets), references, confidence

    ranked = sorted(
        ((rank(values), namespace) for namespace, values in by_namespace.items()),
        key=lambda item: (item[0], item[1]),
        reverse=True,
    )
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def _infer_repository_policy(
    target_directories: tuple[Path, ...],
    requested_namespace: str,
    *,
    namespace_explicit: bool,
) -> _RepositoryPolicy:
    target_names = frozenset(directory.name for directory in target_directories)
    reference_counts = _qualified_repository_reference_counts(
        target_directories,
        requested_namespace,
    )
    observations: list[_RepositoryObservation] = []
    for repository, reference_count in sorted(reference_counts.items()):
        inferred = _infer_repository_target(repository, target_names)
        if inferred is None:
            continue
        target, confidence, score = inferred
        observations.append(
            _RepositoryObservation(target, repository, confidence, score, reference_count)
        )

    ordered_observations = tuple(observations)
    inferred_namespace = (
        None if namespace_explicit else _select_repository_namespace(ordered_observations)
    )
    configured_namespace = (
        requested_namespace if namespace_explicit else (inferred_namespace or requested_namespace)
    )
    by_target: dict[str, list[_RepositoryObservation]] = defaultdict(list)
    for observation in ordered_observations:
        by_target[observation.target].append(observation)

    images: dict[str, ImageConfig] = {}
    review: list[RepositoryInference] = []
    for target, target_observations in sorted(by_target.items()):
        canonical_candidates = [
            observation
            for observation in target_observations
            if _repository_namespace(observation.repository) == configured_namespace
        ]
        canonical = (
            sorted(
                canonical_candidates,
                key=lambda value: (
                    value.reference_count,
                    value.score,
                    value.repository,
                ),
                reverse=True,
            )[0]
            if canonical_candidates
            else None
        )
        default_repository = f"{configured_namespace}/{target}"
        configured_repository = (
            canonical.repository
            if canonical is not None and canonical.repository != default_repository
            else None
        )
        aliases = tuple(
            sorted(
                {
                    observation.repository
                    for observation in target_observations
                    if observation is not canonical
                    and observation.repository.rsplit("/", 1)[-1] != target
                }
            )
        )
        if configured_repository is not None or aliases:
            images[target] = ImageConfig(configured_repository, aliases)

        for observation in target_observations:
            if observation.confidence == "exact":
                continue
            if observation is canonical and configured_repository is not None:
                action = "canonical repository override"
            elif observation.repository in aliases:
                action = "dependency alias"
            else:
                action = "automatic basename match"
            review.append(
                RepositoryInference(
                    target,
                    observation.repository,
                    action,
                    observation.confidence,
                    observation.reference_count,
                )
            )

    source_namespaces = tuple(
        sorted(
            {
                namespace
                for observation in ordered_observations
                if (namespace := _repository_namespace(observation.repository)) is not None
            }
        )
    )
    return _RepositoryPolicy(
        inferred_namespace,
        source_namespaces,
        dict(sorted(images.items())),
        tuple(sorted(review, key=lambda item: (item.target, item.source_repository))),
    )


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


def _image_configuration(images: Mapping[str, ImageConfig]) -> str:
    sections: list[str] = []
    for target, image in sorted(images.items()):
        lines = [f"[images.{json.dumps(target)}]"]
        if image.repository is not None:
            lines.append(f"repository = {json.dumps(image.repository)}")
        if image.aliases:
            lines.append(f"aliases = {_toml_array(image.aliases)}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


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
    images: Mapping[str, ImageConfig],
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
    image_configuration = _image_configuration(images)
    rendered_images = f"\n{image_configuration}\n" if image_configuration else ""
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
{rendered_images}

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
    repository_policy = _infer_repository_policy(
        target_directories,
        requested_namespace,
        namespace_explicit=namespace is not None,
    )
    inferred_namespace = repository_policy.namespace
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
        images=repository_policy.images,
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
        repository_policy.source_namespaces,
        repository_policy.review,
    )
