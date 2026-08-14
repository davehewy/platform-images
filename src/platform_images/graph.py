from __future__ import annotations

import heapq
from collections.abc import Mapping
from dataclasses import dataclass

from platform_images.config import RepositoryConfig
from platform_images.dockerfile import DockerfileParseResult, parse_dockerfile
from platform_images.errors import GraphCycleError
from platform_images.models import ImageReference, ImageTarget, ReferenceKind


@dataclass(frozen=True)
class ImageGraph:
    targets: Mapping[str, ImageTarget]
    dependencies: Mapping[str, frozenset[str]]
    dependents: Mapping[str, frozenset[str]]
    external_dependencies: Mapping[str, tuple[ImageReference, ...]]
    unresolved_references: Mapping[str, tuple[ImageReference, ...]]
    parse_results: Mapping[str, DockerfileParseResult]

    def roots(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, deps in self.dependencies.items() if not deps))

    def direct_dependencies(self, target: str) -> tuple[str, ...]:
        self._require_target(target)
        return tuple(sorted(self.dependencies[target]))

    def direct_dependents(self, target: str) -> tuple[str, ...]:
        self._require_target(target)
        return tuple(sorted(self.dependents[target]))

    def upstream_closure(self, targets: set[str] | frozenset[str]) -> frozenset[str]:
        pending = list(targets)
        result: set[str] = set()
        while pending:
            target = pending.pop()
            self._require_target(target)
            if target in result:
                continue
            result.add(target)
            pending.extend(self.dependencies[target])
        return frozenset(result)

    def downstream_closure(self, targets: set[str] | frozenset[str]) -> frozenset[str]:
        pending = list(targets)
        result: set[str] = set()
        while pending:
            target = pending.pop()
            self._require_target(target)
            if target in result:
                continue
            result.add(target)
            pending.extend(self.dependents[target])
        return frozenset(result)

    def topological_order(
        self, targets: set[str] | frozenset[str] | None = None
    ) -> tuple[str, ...]:
        selected = set(self.targets if targets is None else targets)
        for target in selected:
            self._require_target(target)
        indegree = {
            target: len(self.dependencies[target].intersection(selected)) for target in selected
        }
        ready = [target for target, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        result: list[str] = []
        while ready:
            target = heapq.heappop(ready)
            result.append(target)
            for dependent in self.dependents[target]:
                if dependent not in selected:
                    continue
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(ready, dependent)
        if len(result) != len(selected):
            cycle = self.find_cycle()
            raise GraphCycleError(cycle or tuple(sorted(selected)))
        return tuple(result)

    def find_cycle(self) -> tuple[str, ...] | None:
        visited: set[str] = set()
        active: list[str] = []
        active_set: set[str] = set()

        def visit(target: str) -> tuple[str, ...] | None:
            visited.add(target)
            active.append(target)
            active_set.add(target)
            for dependency in sorted(self.dependencies[target]):
                if dependency in active_set:
                    index = active.index(dependency)
                    return tuple(active[index:] + [dependency])
                if dependency not in visited:
                    cycle = visit(dependency)
                    if cycle:
                        return cycle
            active.pop()
            active_set.remove(target)
            return None

        for target in sorted(self.targets):
            if target not in visited:
                cycle = visit(target)
                if cycle:
                    return cycle
        return None

    def _require_target(self, target: str) -> None:
        if target not in self.targets:
            raise KeyError(f"unknown image target: {target}")


def build_graph(targets: Mapping[str, ImageTarget], config: RepositoryConfig) -> ImageGraph:
    names = frozenset(targets)
    parse_results: dict[str, DockerfileParseResult] = {}
    dependencies: dict[str, frozenset[str]] = {}
    external: dict[str, tuple[ImageReference, ...]] = {}
    unresolved: dict[str, tuple[ImageReference, ...]] = {}
    for name in sorted(targets):
        target = targets[name]
        read_error: str | None = None
        try:
            target.dockerfile.resolve().relative_to(config.root)
            safe_dockerfile = target.dockerfile.is_file()
        except ValueError:
            safe_dockerfile = False
        try:
            text = target.dockerfile.read_text(encoding="utf-8") if safe_dockerfile else ""
        except (OSError, UnicodeDecodeError) as exc:
            text = ""
            read_error = f"unable to read Dockerfile as UTF-8: {exc}"
        result = parse_dockerfile(
            text,
            target_names=names,
            internal_namespace=config.registry.namespace,
            allowed_short_external_images=config.dockerfile.allowed_short_external_images,
        )
        if read_error:
            result = DockerfileParseResult(
                result.references,
                result.stage_aliases,
                result.syntax_errors + ((1, read_error),),
            )
        parse_results[name] = result
        dependencies[name] = frozenset(
            reference.resolved
            for reference in result.references
            if reference.kind is ReferenceKind.LOCAL_TARGET and reference.resolved is not None
        )
        external[name] = tuple(
            sorted(
                (r for r in result.references if r.kind is ReferenceKind.EXTERNAL_IMAGE),
                key=lambda r: (r.resolved or "", r.line_number, r.instruction),
            )
        )
        unresolved[name] = tuple(
            sorted(
                (r for r in result.references if r.kind is ReferenceKind.UNRESOLVED),
                key=lambda r: (r.line_number, r.instruction, r.raw),
            )
        )
    mutable_dependents: dict[str, set[str]] = {name: set() for name in targets}
    for candidate, candidate_dependencies in dependencies.items():
        for dependency in candidate_dependencies:
            mutable_dependents[dependency].add(candidate)
    dependents = {name: frozenset(mutable_dependents[name]) for name in sorted(mutable_dependents)}
    return ImageGraph(
        targets=dict(sorted(targets.items())),
        dependencies=dependencies,
        dependents=dependents,
        external_dependencies=external,
        unresolved_references=unresolved,
        parse_results=parse_results,
    )
