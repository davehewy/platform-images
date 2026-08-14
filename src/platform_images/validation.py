from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from platform_images.config import NAMESPACE_RE, RepositoryConfig
from platform_images.discovery import inspect_targets, valid_target_name
from platform_images.graph import ImageGraph, build_graph
from platform_images.image_identity import (
    build_image_identity_resolver,
    registry_relative_repository,
    repository_name,
)
from platform_images.models import ImageTarget, ReferenceKind


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: str
    message: str
    path: str | None = None
    line: int | None = None
    hint: str | None = None


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]
    graph: ImageGraph

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def valid(self) -> bool:
        return not self.errors


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _manual_mapping_hint(reference: str, target: str) -> str:
    repository = repository_name(reference)
    relative = registry_relative_repository(repository)
    return (
        f"if this is local, add aliases = [{json.dumps(repository)}] under "
        f"[images.{json.dumps(target)}]; if it is also the published destination, set "
        f"repository = {json.dumps(relative)}; "
        f"if it is intentionally external, add external_repositories = "
        f"[{json.dumps(repository)}] under [identity]"
    )


def _build_argument_hint(reference: str) -> str | None:
    names = sorted(
        {
            braced or short
            for braced, short in re.findall(
                r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))",
                reference,
            )
        }
    )
    if not names:
        return None
    examples = ", ".join(f'{name} = "registry.example/team"' for name in names)
    return (
        "set deterministic values used by both dependency parsing and builds under "
        f"[dockerfile.arguments], for example: {examples}"
    )


def validate_repository(config: RepositoryConfig) -> ValidationReport:
    root = config.root
    discovery = inspect_targets(root, config.discovery.roots)
    targets = discovery.targets
    image_identities = build_image_identity_resolver(
        targets,
        config.registry.namespace,
        repositories={name: config.image_repository(name) for name in targets},
        aliases={name: config.image_aliases(name) for name in targets},
        external_repositories=config.identity.external_repositories,
    )
    graph = build_graph(targets, config)
    issues: list[ValidationIssue] = []

    for missing_root in discovery.missing_roots:
        issues.append(
            ValidationIssue(
                "missing-discovery-root",
                "error",
                "configured target discovery root does not exist or is not a directory",
                missing_root,
                hint="create the directory or correct discovery.roots in platform-images.toml",
            )
        )

    if not NAMESPACE_RE.fullmatch(config.registry.namespace):
        issues.append(
            ValidationIssue(
                "invalid-registry-namespace",
                "error",
                f"invalid configured registry namespace: {config.registry.namespace!r}",
                "platform-images.toml",
            )
        )

    for configured_target in sorted(set(config.images) - set(targets)):
        issues.append(
            ValidationIssue(
                "unknown-image-configuration",
                "error",
                f"image identity configuration refers to an undiscovered target: "
                f"{configured_target}",
                "platform-images.toml",
                hint="correct the logical target name or remove the stale images entry",
            )
        )

    for identity, candidates in sorted(image_identities.collisions.items()):
        issues.append(
            ValidationIssue(
                "duplicate-image-identity",
                "error",
                f"image repository or alias maps to multiple local targets: {identity} -> "
                + ", ".join(sorted(candidates)),
                "platform-images.toml",
                hint="make every images.<target>.repository and alias globally unique",
            )
        )

    folded: dict[str, list[tuple[str, ImageTarget]]] = {}
    for target in discovery.entries:
        name = target.name
        folded.setdefault(name.casefold(), []).append((name, target))
        path = _relative(target.directory, root)
        if not valid_target_name(name):
            issues.append(
                ValidationIssue(
                    "invalid-target-name",
                    "error",
                    f"invalid target name: {name}",
                    path,
                )
            )
        build_files = [
            candidate
            for candidate in (target.directory / "Dockerfile", target.directory / "Containerfile")
            if candidate.is_file()
        ]
        if len(build_files) > 1:
            issues.append(
                ValidationIssue(
                    "ambiguous-build-file",
                    "error",
                    "image target directory contains both Dockerfile and Containerfile",
                    path,
                    hint="keep exactly one build file so dependency discovery is unambiguous",
                )
            )
        for field_name, candidate in (
            ("build file", target.dockerfile),
            ("context", target.context),
        ):
            try:
                candidate.resolve().relative_to(root.resolve())
            except ValueError:
                issues.append(
                    ValidationIssue(
                        "path-outside-repository",
                        "error",
                        f"{field_name} path resolves outside the repository",
                        path,
                    )
                )
    for entries in folded.values():
        if len(entries) > 1:
            ordered_entries = sorted(entries, key=lambda entry: _relative(entry[1].directory, root))
            for name, target in ordered_entries:
                issues.append(
                    ValidationIssue(
                        "duplicate-target-name",
                        "error",
                        f"duplicate logical target name (case-insensitive): {name}",
                        _relative(target.directory, root),
                    )
                )

    for target_name in sorted(targets):
        target = targets[target_name]
        dockerfile_path = _relative(target.dockerfile, root)
        for line, message in graph.parse_results[target_name].syntax_errors:
            issues.append(
                ValidationIssue(
                    "invalid-dockerfile-syntax",
                    "error",
                    message,
                    dockerfile_path,
                    line,
                )
            )
        for reference in graph.unresolved_references[target_name]:
            identity_candidates = (
                image_identities.candidates(reference.resolved)
                if reference.resolved is not None
                else frozenset()
            )
            if len(identity_candidates) > 1:
                issues.append(
                    ValidationIssue(
                        "ambiguous-local-reference",
                        "error",
                        f"local image reference maps to multiple targets: {reference.raw} -> "
                        + ", ".join(sorted(identity_candidates)),
                        dockerfile_path,
                        reference.line_number,
                        "make the configured repositories and aliases unique",
                    )
                )
                continue
            internal = reference.resolved is not None and image_identities.is_managed(
                reference.resolved
            )
            if internal:
                matches = image_identities.probable_local_targets(reference.resolved)
                issues.append(
                    ValidationIssue(
                        "missing-internal-target",
                        "error",
                        f"internal image target does not exist: {reference.raw}",
                        dockerfile_path,
                        reference.line_number,
                        _manual_mapping_hint(reference.resolved, matches[0]) if matches else None,
                    )
                )
            elif reference.resolved is not None and "/" not in reference.resolved.split("@", 1)[0]:
                candidate = reference.resolved.split("@", 1)[0].split(":", 1)[0]
                matches = difflib.get_close_matches(candidate, targets, n=1)
                issues.append(
                    ValidationIssue(
                        "unknown-short-reference",
                        "error",
                        "unqualified image reference is neither a local target nor an allowed "
                        f"external image: {reference.raw}",
                        dockerfile_path,
                        reference.line_number,
                        (
                            f"possible local target: {matches[0]}"
                            if matches
                            else "qualify the external registry or add its repository name to "
                            "dockerfile.allowed_short_external_images"
                        ),
                    )
                )
            else:
                issues.append(
                    ValidationIssue(
                        "unresolved-reference",
                        "error",
                        f"unresolved {reference.instruction} reference: {reference.raw}",
                        dockerfile_path,
                        reference.line_number,
                        _build_argument_hint(reference.raw),
                    )
                )
        for reference in graph.external_dependencies[target_name]:
            if reference.resolved is None:
                continue
            matches = image_identities.probable_local_targets(reference.resolved)
            if not matches:
                continue
            issues.append(
                ValidationIssue(
                    "probable-local-reference",
                    "warning",
                    f"qualified image reference resembles local target {matches[0]!r} but is "
                    f"currently external: {reference.raw}",
                    dockerfile_path,
                    reference.line_number,
                    _manual_mapping_hint(reference.resolved, matches[0]),
                )
            )
        for alias, line in graph.parse_results[target_name].stage_aliases:
            if alias in targets:
                issues.append(
                    ValidationIssue(
                        "stage-target-collision",
                        "error",
                        f'stage alias "{alias}" collides with a local image target',
                        dockerfile_path,
                        line,
                    )
                )
        # A local reference must have survived graph construction as an existing node.
        for reference in graph.parse_results[target_name].references:
            if reference.kind is ReferenceKind.LOCAL_TARGET and reference.resolved not in targets:
                issues.append(
                    ValidationIssue(
                        "ambiguous-local-reference",
                        "error",
                        f"cannot unambiguously bind local reference: {reference.raw}",
                        dockerfile_path,
                        reference.line_number,
                    )
                )

    cycle = graph.find_cycle()
    if cycle:
        issues.append(
            ValidationIssue(
                "dependency-cycle",
                "error",
                "local image dependency cycle detected: " + " -> ".join(cycle),
            )
        )
    ordered = tuple(
        sorted(
            issues,
            key=lambda issue: (
                0 if issue.severity == "error" else 1,
                issue.path or "",
                issue.line or 0,
                issue.code,
                issue.message,
            ),
        )
    )
    return ValidationReport(ordered, graph)
