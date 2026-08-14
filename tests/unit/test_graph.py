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


def test_multi_parent_tree_repeats_edge_and_marks_target(
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
    assert "(*) target has more than one local dependency" in rendered


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
