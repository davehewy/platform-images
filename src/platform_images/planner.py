from __future__ import annotations

from collections import deque
from pathlib import Path

from platform_images.changes import ChangeSet, validate_removed_references
from platform_images.config import RepositoryConfig
from platform_images.errors import PlatformImagesError
from platform_images.graph import ImageGraph
from platform_images.models import BuildMode, BuildPlan, BuildPlanTarget
from platform_images.references import ReferencePolicy
from platform_images.registry import RegistryClient


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _dependency_reasons(mask: int, selected_names: tuple[str, ...]) -> tuple[str, ...]:
    reasons: list[str] = []
    while mask:
        selected_bit = mask & -mask
        index = selected_bit.bit_length() - 1
        reasons.append(f"dependency-of:{selected_names[index]}")
        mask ^= selected_bit
    return tuple(reasons)


def local_plan(
    graph: ImageGraph,
    config: RepositoryConfig,
    selected: frozenset[str],
    *,
    include_dependencies: bool = True,
) -> BuildPlan:
    build_targets = graph.upstream_closure(selected) if include_dependencies else selected
    order = graph.topological_order(build_targets)
    selected_names = tuple(sorted(selected))
    selected_bits = {name: 1 << index for index, name in enumerate(selected_names)}
    consumer_masks = {name: selected_bits.get(name, 0) for name in build_targets}
    for consumer in reversed(order):
        for dependency in graph.dependencies[consumer]:
            if dependency not in build_targets:
                continue
            consumer_masks[dependency] |= consumer_masks[consumer]
    policy = ReferencePolicy(config)
    plan_targets: list[BuildPlanTarget] = []
    for name in order:
        target = graph.targets[name]
        dependencies = graph.direct_dependencies(name)
        if name in selected:
            reasons = ("selected",)
        else:
            reasons = _dependency_reasons(consumer_masks[name], selected_names)
        input_refs = {dep: policy.local(dep) for dep in dependencies}
        plan_targets.append(
            BuildPlanTarget(
                name=name,
                reasons=reasons,
                dependencies=dependencies,
                needs=tuple(dep for dep in dependencies if dep in build_targets),
                dockerfile=_relative(target.dockerfile, config.root),
                context=_relative(target.context, config.root),
                output_ref=policy.local(name),
                input_refs=input_refs,
                push=False,
                build_contexts=graph.build_contexts(name, input_refs),
            )
        )
    return BuildPlan(1, BuildMode.LOCAL, None, "local", tuple(plan_targets), ())


def affected_reasons(graph: ImageGraph, changes: ChangeSet) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {
        target: set(changes.reasons.get(target, ())) for target in changes.changed_targets
    }
    queue: deque[str] = deque(sorted(changes.changed_targets))
    while queue:
        source = queue.popleft()
        for dependent in graph.direct_dependents(source):
            reason = f"dependent-of:{source}"
            first_seen = dependent not in result
            result.setdefault(dependent, set()).add(reason)
            if first_seen:
                queue.append(dependent)
    return {name: tuple(sorted(values)) for name, values in sorted(result.items())}


def change_plan(
    graph: ImageGraph,
    config: RepositoryConfig,
    changes: ChangeSet,
    *,
    base_sha: str | None,
    head_sha: str,
    mode: BuildMode,
    registry: str,
    pipeline_id: str | None,
    registry_client: RegistryClient,
) -> BuildPlan:
    if mode is BuildMode.LOCAL:
        raise ValueError("change plans require a CI build mode")
    validate_removed_references(graph, changes, config.root)
    reasons = affected_reasons(graph, changes)
    build_targets = frozenset(reasons)
    order = graph.topological_order(build_targets)
    policy = ReferencePolicy(config, registry, pipeline_id)
    outputs = {name: policy.output(name, mode, head_sha) for name in order}
    plan_targets: list[BuildPlanTarget] = []
    for name in order:
        target = graph.targets[name]
        dependencies = graph.direct_dependencies(name)
        input_refs = {
            dependency: (
                outputs[dependency]
                if dependency in build_targets
                else registry_client.resolve_stable(dependency)
            )
            for dependency in dependencies
        }
        plan_targets.append(
            BuildPlanTarget(
                name=name,
                reasons=reasons[name],
                dependencies=dependencies,
                needs=tuple(dep for dep in dependencies if dep in build_targets),
                dockerfile=_relative(target.dockerfile, config.root),
                context=_relative(target.context, config.root),
                output_ref=outputs[name],
                input_refs=input_refs,
                push=True,
                build_contexts=graph.build_contexts(name, input_refs),
            )
        )
    return BuildPlan(
        1,
        mode,
        base_sha,
        head_sha,
        tuple(plan_targets),
        tuple(sorted(changes.removed_targets)),
    )


def all_ci_plan(
    graph: ImageGraph,
    config: RepositoryConfig,
    *,
    head_sha: str,
    mode: BuildMode,
    registry: str,
    pipeline_id: str | None,
) -> BuildPlan:
    changes = ChangeSet(
        changed_files=(),
        changed_targets=frozenset(graph.targets),
        removed_targets=frozenset(),
        global_change=True,
        reasons={name: ("all-images",) for name in sorted(graph.targets)},
    )
    # All dependencies are built, so this resolver must never be queried.
    return change_plan(
        graph,
        config,
        changes,
        base_sha=None,
        head_sha=head_sha,
        mode=mode,
        registry=registry,
        pipeline_id=pipeline_id,
        registry_client=_UnreachableRegistryClient(),
    )


class _UnreachableRegistryClient(RegistryClient):
    def resolve_stable(self, target: str) -> str:
        raise AssertionError(f"unexpected stable dependency lookup for {target}")


def validate_plan_against_graph(
    plan: BuildPlan, graph: ImageGraph, config: RepositoryConfig
) -> None:
    """Reject a stale or altered persisted plan before it becomes executable CI YAML."""
    names = tuple(target.name for target in plan.targets)
    if len(names) != len(set(names)):
        raise PlatformImagesError("persisted plan contains duplicate image targets")
    unknown = sorted(set(names) - set(graph.targets))
    if unknown:
        raise PlatformImagesError(
            "persisted plan contains unknown image targets: " + ", ".join(unknown)
        )
    expected_order = graph.topological_order(frozenset(names))
    if names != expected_order:
        raise PlatformImagesError(
            "persisted plan target order is not the graph's deterministic topological order"
        )
    selected = set(names)
    for target in plan.targets:
        discovered = graph.targets[target.name]
        expected_dependencies = graph.direct_dependencies(target.name)
        expected_needs = tuple(
            dependency for dependency in expected_dependencies if dependency in selected
        )
        expected_dockerfile = _relative(discovered.dockerfile, config.root)
        expected_context = _relative(discovered.context, config.root)
        if target.dependencies != expected_dependencies:
            raise PlatformImagesError(
                f'persisted plan dependencies do not match graph for "{target.name}"'
            )
        if target.needs != expected_needs:
            raise PlatformImagesError(
                f'persisted plan needs do not match graph for "{target.name}"'
            )
        if set(target.input_refs) != set(expected_dependencies):
            raise PlatformImagesError(
                f'persisted plan input references do not match graph for "{target.name}"'
            )
        if target.build_contexts != graph.build_contexts(target.name, target.input_refs):
            raise PlatformImagesError(
                f'persisted plan build contexts do not match graph for "{target.name}"'
            )
        if (target.dockerfile, target.context) != (expected_dockerfile, expected_context):
            raise PlatformImagesError(
                f'persisted plan paths do not match discovery for "{target.name}"'
            )
        if not target.output_ref:
            raise PlatformImagesError(
                f'persisted plan has an empty output reference for "{target.name}"'
            )
        if plan.mode is not BuildMode.LOCAL and not target.push:
            raise PlatformImagesError(f'persisted CI plan disables push for "{target.name}"')
