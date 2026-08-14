from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from platform_images.config import RepositoryConfig
from platform_images.discovery import discover_targets
from platform_images.errors import GraphCycleError
from platform_images.graph import build_graph
from platform_images.renderers.graph_json import render_graph_json
from platform_images.renderers.graph_text import render_graph_text


def graph_for(root: Path):
    config = RepositoryConfig.load(root)
    return build_graph(discover_targets(root), config)


def test_corrected_api_base_curl_jq_semantics(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    graph = graph_for(
        repository_factory(
            {
                "api": "FROM alpine\n",
                "base": "FROM api\n",
                "curl": "FROM base\n",
                "jq": "FROM base\n",
            }
        )
    )
    assert graph.dependencies == {
        "api": frozenset(),
        "base": frozenset({"api"}),
        "curl": frozenset({"base"}),
        "jq": frozenset({"base"}),
    }
    assert graph.dependents["api"] == frozenset({"base"})
    assert graph.dependents["base"] == frozenset({"curl", "jq"})
    assert graph.roots() == ("api",)
    assert graph.topological_order() == ("api", "base", "curl", "jq")


def test_disconnected_images_are_sorted(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    graph = graph_for(repository_factory({"z": "FROM alpine\n", "a": "FROM busybox\n"}))
    assert graph.roots() == ("a", "z")
    assert graph.topological_order() == ("a", "z")
    assert render_graph_text(graph) == ("Container image dependency graph\n├── a\n└── z")


def test_ascii_tree_has_visible_root_and_dependency_connectors(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    graph = graph_for(repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"}))

    assert render_graph_text(graph, ascii_only=True) == (
        "Container image dependency graph\n\\-- base\n    \\-- curl"
    )


def test_upstream_and_downstream_closures(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    graph = graph_for(
        repository_factory({"api": "FROM alpine\n", "base": "FROM api\n", "curl": "FROM base\n"})
    )
    assert graph.upstream_closure(frozenset({"curl"})) == {"api", "base", "curl"}
    assert graph.downstream_closure(frozenset({"api"})) == {"api", "base", "curl"}


def test_multi_parent_tree_retains_every_edge_and_marks_repeated_target(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    graph = graph_for(
        repository_factory(
            {
                "builder-base": "FROM alpine\n",
                "runtime-base": "FROM alpine\n",
                "application": "FROM builder-base\nCOPY --from=runtime-base /x /x\n",
            }
        )
    )
    rendered = render_graph_text(graph)
    assert rendered.count("application (*)") == 2
    assert rendered.count("application (*) [already shown]") == 1
    assert "(*) target has more than one local dependency" in rendered
    assert "[already shown] incoming edge retained; repeated subtree omitted" in rendered


def test_tree_renderer_is_stack_safe_for_more_than_one_thousand_levels(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    images = {"node-0000": "FROM alpine\n"}
    images.update({f"node-{index:04d}": f"FROM node-{index - 1:04d}\n" for index in range(1, 1200)})

    rendered = render_graph_text(graph_for(repository_factory(images)), ascii_only=True)

    assert len(rendered.splitlines()) == 1201
    assert rendered.startswith("Container image dependency graph\n\\-- node-0000\n")
    assert rendered.endswith("\\-- node-1199")


def test_cycle_detection_is_stack_safe_for_more_than_one_thousand_levels(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    images = {"node-0000": "FROM node-1199\n"}
    images.update({f"node-{index:04d}": f"FROM node-{index - 1:04d}\n" for index in range(1, 1200)})
    graph = graph_for(repository_factory(images))

    cycle = graph.find_cycle()

    assert cycle is not None
    assert len(cycle) == 1201
    assert cycle[:3] == ("node-0000", "node-1199", "node-1198")
    assert cycle[-1] == "node-0000"
    with pytest.raises(GraphCycleError):
        graph.topological_order()


def test_cycle_has_complete_deterministic_path(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    graph = graph_for(
        repository_factory({"base": "FROM curl\n", "curl": "FROM tools\n", "tools": "FROM base\n"})
    )
    assert graph.find_cycle() == ("base", "curl", "tools", "base")
    with pytest.raises(GraphCycleError, match="base -> curl -> tools -> base"):
        graph.topological_order()


def test_json_schema_and_order(repository_factory: Callable[[dict[str, str]], Path]) -> None:
    root = repository_factory({"curl": "FROM base\n", "base": "FROM alpine:3.22\n"})
    data = json.loads(render_graph_json(graph_for(root), root))
    assert data["schema_version"] == 1
    assert list(data["targets"]) == ["base", "curl"]
    assert data["targets"]["base"]["dependents"] == ["curl"]
    assert data["targets"]["base"]["external_dependencies"] == ["alpine:3.22"]
    assert data["targets"]["curl"]["local_references"] == [
        {
            "source": "base",
            "target": "base",
            "instruction": "FROM",
            "line": 1,
        }
    ]
