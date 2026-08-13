from __future__ import annotations

import json
from pathlib import Path

from platform_images.graph import ImageGraph


def graph_data(graph: ImageGraph, root: Path) -> dict[str, object]:
    targets: dict[str, object] = {}
    for name in sorted(graph.targets):
        target = graph.targets[name]
        targets[name] = {
            "dependencies": list(graph.direct_dependencies(name)),
            "dependents": list(graph.direct_dependents(name)),
            "external_dependencies": [
                reference.resolved or reference.raw
                for reference in graph.external_dependencies[name]
            ],
            "dockerfile": target.dockerfile.relative_to(root).as_posix(),
        }
    return {"schema_version": 1, "targets": targets}


def render_graph_json(graph: ImageGraph, root: Path) -> str:
    return json.dumps(graph_data(graph, root), indent=2, sort_keys=False)
