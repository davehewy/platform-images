from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from platform_images.changes import map_changes
from platform_images.config import RepositoryConfig
from platform_images.discovery import discover_targets
from platform_images.errors import MissingStableImageError
from platform_images.graph import build_graph
from platform_images.models import BuildMode, ChangedPath
from platform_images.planner import affected_reasons, change_plan, local_plan
from platform_images.registry import StaticRegistryClient


def state(root: Path):
    config = RepositoryConfig.load(root)
    graph = build_graph(discover_targets(root), config)
    return config, graph


def test_local_plan_is_topological_and_exact(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    config, graph = state(root)
    plan = local_plan(graph, config, frozenset({"curl"}))
    assert [target.name for target in plan.targets] == ["base", "curl"]
    base, curl = plan.targets
    assert base.reasons == ("dependency-of:curl",)
    assert base.needs == ()
    assert curl.reasons == ("selected",)
    assert curl.dependencies == ("base",)
    assert curl.needs == ("base",)
    assert curl.input_refs == {"base": "localhost/platform-images/base:dev"}
    assert not curl.push


def test_no_deps_keeps_input_binding_but_not_need(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    config, graph = state(root)
    plan = local_plan(graph, config, frozenset({"curl"}), include_dependencies=False)
    assert [target.name for target in plan.targets] == ["curl"]
    assert plan.targets[0].needs == ()
    assert plan.targets[0].input_refs["base"].endswith("/base:dev")


def test_changed_base_affects_all_downstream_with_in_plan_needs(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n", "jq": "FROM base\n"})
    config, graph = state(root)
    changes = map_changes(
        (ChangedPath("M", None, "images/base/Dockerfile"),), graph.targets, config
    )
    assert affected_reasons(graph, changes) == {
        "base": ("source-changed:images/base/Dockerfile",),
        "curl": ("dependent-of:base",),
        "jq": ("dependent-of:base",),
    }
    plan = change_plan(
        graph,
        config,
        changes,
        base_sha="a" * 40,
        head_sha="b" * 40,
        mode=BuildMode.MERGE_REQUEST,
        registry="registry.example.com",
        pipeline_id="123",
        registry_client=StaticRegistryClient({}),
    )
    assert [target.name for target in plan.targets] == ["base", "curl", "jq"]
    curl = plan.targets[1]
    assert curl.needs == ("base",)
    assert curl.input_refs["base"] == plan.targets[0].output_ref
    assert curl.output_ref.endswith(f":ci-123-{'b' * 40}")


def test_only_curl_uses_stable_base_digest(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    config, graph = state(root)
    changes = map_changes(
        (ChangedPath("M", None, "images/curl/install.sh"),), graph.targets, config
    )
    stable = "registry.example.com/platform-images/base@sha256:stable"
    plan = change_plan(
        graph,
        config,
        changes,
        base_sha="base",
        head_sha="head",
        mode=BuildMode.MERGE_REQUEST,
        registry="registry.example.com",
        pipeline_id="7",
        registry_client=StaticRegistryClient({"base": stable}),
    )
    assert [target.name for target in plan.targets] == ["curl"]
    assert plan.targets[0].needs == ()
    assert plan.targets[0].input_refs == {"base": stable}


def test_missing_stable_dependency_does_not_add_upstream_build(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    config, graph = state(root)
    changes = map_changes(
        (ChangedPath("M", None, "images/curl/Dockerfile"),), graph.targets, config
    )
    with pytest.raises(MissingStableImageError, match="complete default-branch bootstrap"):
        change_plan(
            graph,
            config,
            changes,
            base_sha="base",
            head_sha="head",
            mode=BuildMode.MERGE_REQUEST,
            registry="registry.example.com",
            pipeline_id="7",
            registry_client=StaticRegistryClient({}),
        )


def test_default_branch_references_are_immutable_sha_tags(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config, graph = state(root)
    changes = map_changes(
        (ChangedPath("M", None, "images/base/Dockerfile"),), graph.targets, config
    )
    plan = change_plan(
        graph,
        config,
        changes,
        base_sha="old",
        head_sha="fullcommit",
        mode=BuildMode.DEFAULT_BRANCH,
        registry="registry.example.com",
        pipeline_id="9",
        registry_client=StaticRegistryClient({}),
    )
    assert plan.targets[0].output_ref.endswith(":sha-fullcommit")
