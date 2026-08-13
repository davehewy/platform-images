from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from platform_images.config import NAMESPACE_RE, RepositoryConfig
from platform_images.discovery import discover_targets, valid_target_name
from platform_images.graph import ImageGraph, build_graph
from platform_images.models import ReferenceKind


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


def validate_repository(config: RepositoryConfig) -> ValidationReport:
    root = config.root
    targets = discover_targets(root)
    graph = build_graph(targets, config)
    issues: list[ValidationIssue] = []

    if not NAMESPACE_RE.fullmatch(config.registry.namespace):
        issues.append(
            ValidationIssue(
                "invalid-registry-namespace",
                "error",
                f"invalid configured registry namespace: {config.registry.namespace!r}",
                "platform-images.toml",
            )
        )

    folded: dict[str, list[str]] = {}
    for name, target in targets.items():
        folded.setdefault(name.casefold(), []).append(name)
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
        if not target.dockerfile.is_file():
            issues.append(
                ValidationIssue(
                    "missing-dockerfile",
                    "error",
                    "image target directory does not contain Dockerfile",
                    path,
                )
            )
        for field_name, candidate in (
            ("Dockerfile", target.dockerfile),
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
    for names in folded.values():
        if len(names) > 1:
            for name in sorted(names):
                issues.append(
                    ValidationIssue(
                        "duplicate-target-name",
                        "error",
                        f"duplicate logical target name (case-insensitive): {name}",
                        f"images/{name}",
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
            internal = (
                reference.resolved is not None and config.registry.namespace in reference.resolved
            )
            if internal:
                candidate = reference.resolved.rsplit("/", 1)[-1].split(":", 1)[0]
                matches = difflib.get_close_matches(candidate, targets, n=1)
                issues.append(
                    ValidationIssue(
                        "missing-internal-target",
                        "error",
                        f"internal image target does not exist: {reference.raw}",
                        dockerfile_path,
                        reference.line_number,
                        f"possible match: {matches[0]}" if matches else None,
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
