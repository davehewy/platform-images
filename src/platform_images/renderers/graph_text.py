from __future__ import annotations

from platform_images.graph import ImageGraph


def render_graph_text(graph: ImageGraph) -> str:
    if not graph.targets:
        return "No image targets discovered."
    cycle = graph.find_cycle()
    if cycle:
        return "local image dependency cycle detected:\n" + " -> ".join(cycle)
    lines: list[str] = []

    def render_children(target: str, prefixes: tuple[bool, ...], path: frozenset[str]) -> None:
        children = graph.direct_dependents(target)
        for index, child in enumerate(children):
            last = index == len(children) - 1
            prefix = "".join("    " if parent_last else "│   " for parent_last in prefixes)
            marker = " (*)" if len(graph.dependencies[child]) > 1 else ""
            lines.append(f"{prefix}{'└── ' if last else '├── '}{child}{marker}")
            if child not in path:
                render_children(child, (*prefixes, last), path | {child})

    roots = graph.roots()
    for root_index, root in enumerate(roots):
        if root_index:
            lines.append("")
        lines.append(root)
        render_children(root, (), frozenset({root}))
    if any(len(dependencies) > 1 for dependencies in graph.dependencies.values()):
        lines.extend(["", "(*) target has more than one local dependency"])
    return "\n".join(lines)
