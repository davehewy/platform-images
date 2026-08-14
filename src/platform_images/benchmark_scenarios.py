"""Deterministic repository generators for graph benchmarks and stress tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

IMPERFECT_DISCOVERY_ROOTS = (
    "containers/shared",
    "services/payments/images",
    "platform/runtime/containers",
    "utils",
    "utils/container-images",
    "teams/data/images",
    "build/oci/catalog",
)
LAYER_WIDTH = 15
ROLES = (
    "foundation",
    "runtime",
    "toolchain",
    "service",
    "worker",
    "application",
)


@dataclass(frozen=True)
class ImperfectRepository:
    """Expected structure and graph for one generated adversarial repository."""

    locations: dict[str, str]
    dependencies: dict[str, frozenset[str]]
    probable_local_warnings: dict[str, tuple[str, str]]
    explicit_external_references: dict[str, str]
    ignored_directories: tuple[str, ...]
    dockerfile_count: int
    containerfile_count: int
    layer_width: int = LAYER_WIDTH

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self.locations)

    @property
    def root_target(self) -> str:
        return self.names[0]

    @property
    def leaf_target(self) -> str:
        return self.names[-1]


def target_name(index: int) -> str:
    layer = index // LAYER_WIDTH
    return f"{ROLES[layer % len(ROLES)]}-{index:03d}"


def _dependency_indexes(index: int) -> frozenset[int]:
    layer, slot = divmod(index, LAYER_WIDTH)
    if layer == 0:
        return frozenset() if index == 0 else frozenset({0})
    previous_start = (layer - 1) * LAYER_WIDTH
    dependencies = {previous_start + slot}
    # Every lane in layer one shares the same foundation. That creates a genuine wide blast radius
    # without turning the whole repository into an unrealistic 300-level serial chain.
    if layer == 1:
        dependencies.add(0)
    if slot % 2 == 0:
        dependencies.add(previous_start + ((slot - 1) % LAYER_WIDTH))
    if layer >= 2 and slot % 3 == 0:
        dependencies.add((layer - 2) * LAYER_WIDTH + ((slot + layer) % LAYER_WIDTH))
    if layer % 4 == 0:
        dependencies.add(previous_start)
    return frozenset(dependency for dependency in dependencies if dependency < index)


def _repository_for(index: int, name: str) -> str | None:
    if index % 37:
        return None
    role = name.rsplit("-", 1)[0]
    return f"published/{role}/{name}-oci"


def _aliases_for(index: int, name: str) -> tuple[str, ...]:
    if index % 29:
        return ()
    aliases = [f"nexus.example.com/legacy/base-images/{name}-legacy"]
    if index % 58 == 0:
        aliases.append(f"registry.gitlab.example:5050/archive/{name}-v1")
    return tuple(aliases)


def _local_reference(
    consumer_index: int,
    dependency_index: int,
    names: tuple[str, ...],
    repositories: dict[str, str],
    aliases: dict[str, tuple[str, ...]],
) -> str:
    dependency = names[dependency_index]
    configured_repository = repositories.get(dependency)
    if configured_repository is not None and (consumer_index + dependency_index) % 3 == 0:
        return f"nexus.example.com/{configured_repository}:stable"
    configured_aliases = aliases.get(dependency, ())
    if configured_aliases and (consumer_index + dependency_index) % 4 == 0:
        return f"{configured_aliases[0]}:supported"
    style = (consumer_index * 7 + dependency_index * 3) % 5
    if style == 0:
        return dependency
    if style == 1:
        return f"${{SOURCE_REGISTRY}}/{dependency}:main"
    if style == 2:
        return f"nexus.example.com/platform/runtime/{dependency}:latest"
    if style == 3:
        return f"ghcr.io/acme-platform/{dependency}@sha256:{dependency_index:064x}"
    return f"registry.gitlab.example:5050/platform/images/{dependency}:sha-deadbeef"


def _dockerfile_text(
    index: int,
    names: tuple[str, ...],
    dependency_indexes: frozenset[int],
    repositories: dict[str, str],
    aliases: dict[str, tuple[str, ...]],
    warning: tuple[str, str] | None,
    explicit_external: str | None,
) -> str:
    references = [
        _local_reference(index, dependency, names, repositories, aliases)
        for dependency in sorted(dependency_indexes)
    ]
    needs_source_registry = any("${SOURCE_REGISTRY}" in reference for reference in references)
    lines = ["# syntax=docker/dockerfile:1.7"]
    if needs_source_registry:
        lines.append("ARG SOURCE_REGISTRY=registry.invalid/ignored-default")
    if references:
        lines.append(f"FROM --platform=$TARGETPLATFORM {references[0]} AS upstream")
    else:
        lines.append("FROM docker.io/library/alpine:3.22 AS upstream")
    lines.extend(
        [
            "RUN printf 'target=%s\\n' \"$TARGETPLATFORM\" > /target",
            "FROM docker.io/library/busybox:1.37 AS packaging",
            "COPY --from=upstream /target /work/target",
            "COPY --from=0 /target /work/stage-zero-target",
        ]
    )
    for offset, reference in enumerate(references[1:], 1):
        if offset % 3 == 1:
            lines.append(f"COPY --from={reference} /etc/os-release /work/dependency-{offset}")
        elif offset % 3 == 2:
            lines.append(f"ADD --from {reference} /etc/group /work/group-{offset}")
        else:
            lines.append(
                "RUN --mount=type=bind,from="
                f"{reference},source=/etc,target=/mnt/etc test -f /mnt/etc/os-release"
            )
    if warning is not None:
        lines.append(f"COPY --from={warning[0]} /usr/bin/retired-helper /work/retired-helper")
    if explicit_external is not None:
        lines.append(
            f"COPY --from={explicit_external}:stable /usr/bin/vendor-helper /work/vendor-helper"
        )
    if index % 17 == 0:
        lines.extend(
            [
                "RUN <<'NOT_A_DOCKERFILE'",
                "FROM fake.example/inside-heredoc:latest",
                "COPY --from=also-not-a-reference /tmp/a /tmp/b",
                "NOT_A_DOCKERFILE",
            ]
        )
    lines.extend(
        [
            "FROM scratch AS final",
            "COPY --from=packaging /work /work",
            'LABEL org.opencontainers.image.title="generated stress target"',
        ]
    )
    return "\n".join(lines) + "\n"


def write_imperfect_repository(root: Path, image_count: int = 360) -> ImperfectRepository:
    """Create a valid but noisy, registry-heavy layered DAG with deterministic warnings."""
    if image_count < LAYER_WIDTH * 2:
        raise ValueError(f"the imperfect scenario requires at least {LAYER_WIDTH * 2} images")
    names = tuple(target_name(index) for index in range(image_count))
    repositories = {
        name: repository
        for index, name in enumerate(names)
        if (repository := _repository_for(index, name)) is not None
    }
    aliases = {
        name: configured
        for index, name in enumerate(names)
        if (configured := _aliases_for(index, name))
    }

    warning_specs: dict[int, tuple[str, str]] = {}
    for consumer_index in range(LAYER_WIDTH * 2 + 1, image_count, 43):
        likely_index = consumer_index - LAYER_WIDTH
        likely_target = names[likely_index]
        compact_name = likely_target.replace("-", "")
        warning_specs[consumer_index] = (
            f"nexus.example.com/retired/{compact_name}:latest",
            likely_target,
        )

    external_specs: dict[int, str] = {}
    for target_index in range(7, image_count, 89):
        consumer_index = (target_index + 23) % image_count
        external_specs[consumer_index] = f"ghcr.io/vendor/{names[target_index]}"
    external_repositories = tuple(sorted(set(external_specs.values())))

    roots_toml = ",\n  ".join(json.dumps(value) for value in IMPERFECT_DISCOVERY_ROOTS)
    external_toml = ",\n  ".join(json.dumps(value) for value in external_repositories)
    config_lines = [
        "[registry]",
        'namespace = "platform/base-images"',
        'registry_environment_variable = "PLATFORM_IMAGES_REGISTRY"',
        'stable_tag = "main"',
        'transport = "docker"',
        'provider = "oci"',
        'authentication = "ambient"',
        "",
        "[tags]",
        'ci_prefix = "ci"',
        'commit_prefix = "sha"',
        "",
        "[build]",
        'backend = "docker"',
        "",
        "[dockerfile]",
        'allowed_short_external_images = ["alpine", "busybox"]',
        'arguments = { SOURCE_REGISTRY = "registry.gitlab.example:5050/platform/images" }',
        "",
        "[identity]",
        "external_repositories = [",
        f"  {external_toml}" if external_toml else "",
        "]",
        "",
        "[changes]",
        'global_inputs = ["build/policy/**", "shared/scripts/**"]',
        "",
        "[discovery]",
        "roots = [",
        f"  {roots_toml}",
        "]",
        "",
    ]
    for name in names:
        repository = repositories.get(name)
        configured_aliases = aliases.get(name, ())
        if repository is None and not configured_aliases:
            continue
        config_lines.append(f"[images.{json.dumps(name)}]")
        if repository is not None:
            config_lines.append(f"repository = {json.dumps(repository)}")
        if configured_aliases:
            rendered_aliases = ", ".join(json.dumps(value) for value in configured_aliases)
            config_lines.append(f"aliases = [{rendered_aliases}]")
        config_lines.append("")
    (root / "platform-images.toml").write_text("\n".join(config_lines), encoding="utf-8")

    ignored_directories: list[str] = []
    for discovery_root in IMPERFECT_DISCOVERY_ROOTS:
        for index in range(4):
            directory = root / discovery_root / f"not-an-image-{index}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "README.md").write_text(
                "This directory deliberately has no Dockerfile or Containerfile.\n",
                encoding="utf-8",
            )
            ignored_directories.append(directory.relative_to(root).as_posix())

    locations: dict[str, str] = {}
    expected_dependencies: dict[str, frozenset[str]] = {}
    probable_local_warnings: dict[str, tuple[str, str]] = {}
    explicit_external_references: dict[str, str] = {}
    dockerfile_count = 0
    containerfile_count = 0
    for index, name in enumerate(names):
        discovery_root = IMPERFECT_DISCOVERY_ROOTS[index % len(IMPERFECT_DISCOVERY_ROOTS)]
        directory = root / discovery_root / name
        directory.mkdir(parents=True, exist_ok=True)
        dependency_indexes = _dependency_indexes(index)
        warning = warning_specs.get(index)
        explicit_external = external_specs.get(index)
        filename = "Containerfile" if index % 3 == 1 else "Dockerfile"
        if filename == "Dockerfile":
            dockerfile_count += 1
        else:
            containerfile_count += 1
        (directory / filename).write_text(
            _dockerfile_text(
                index,
                names,
                dependency_indexes,
                repositories,
                aliases,
                warning,
                explicit_external,
            ),
            encoding="utf-8",
        )
        locations[name] = directory.relative_to(root).as_posix()
        expected_dependencies[name] = frozenset(names[value] for value in dependency_indexes)
        if warning is not None:
            probable_local_warnings[name] = warning
        if explicit_external is not None:
            explicit_external_references[name] = explicit_external

    return ImperfectRepository(
        locations,
        expected_dependencies,
        probable_local_warnings,
        explicit_external_references,
        tuple(sorted(ignored_directories)),
        dockerfile_count,
        containerfile_count,
    )
