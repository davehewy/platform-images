from __future__ import annotations

import json
from pathlib import Path

from platform_images.config import RepositoryConfig
from platform_images.graph import ImageGraph
from platform_images.models import ReferenceKind


def graph_data(
    graph: ImageGraph,
    root: Path,
    config: RepositoryConfig | None = None,
) -> dict[str, object]:
    targets: dict[str, object] = {}
    for name in sorted(graph.targets):
        target = graph.targets[name]
        data = {
            "dependencies": list(graph.direct_dependencies(name)),
            "dependents": list(graph.direct_dependents(name)),
            "external_dependencies": [
                reference.resolved or reference.raw
                for reference in graph.external_dependencies[name]
            ],
            "local_references": [
                {
                    "source": reference.source or reference.raw,
                    "target": reference.resolved,
                    "instruction": reference.instruction,
                    "line": reference.line_number,
                }
                for reference in graph.parse_results[name].references
                if reference.kind is ReferenceKind.LOCAL_TARGET
            ],
            "dockerfile": target.dockerfile.relative_to(root).as_posix(),
        }
        if config is not None:
            data["repository"] = config.image_repository(name)
            data["aliases"] = list(config.image_aliases(name))
        targets[name] = data
    return {"schema_version": 1, "targets": targets}


def render_graph_json(
    graph: ImageGraph,
    root: Path,
    config: RepositoryConfig | None = None,
) -> str:
    return json.dumps(graph_data(graph, root, config), indent=2, sort_keys=False)
