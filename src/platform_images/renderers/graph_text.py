from __future__ import annotations

from platform_images.graph import ImageGraph


def render_graph_text(graph: ImageGraph, *, ascii_only: bool = False) -> str:
    if not graph.targets:
        return "No image targets discovered."
    cycle = graph.find_cycle()
    if cycle:
        return "local image dependency cycle detected:\n" + " -> ".join(cycle)
    lines = ["Container image dependency graph"]
    branches = ("+-- ", "\\-- ") if ascii_only else ("├── ", "└── ")
    vertical = "|   " if ascii_only else "│   "
    expanded: set[str] = set()
    repeated_target = False

    def render_children(target: str, prefixes: tuple[bool, ...]) -> None:
        nonlocal repeated_target
        children = graph.direct_dependents(target)
        stack = [
            (child, prefixes, index == len(children) - 1)
            for index, child in reversed(tuple(enumerate(children)))
        ]
        while stack:
            child, parent_prefixes, last = stack.pop()
            prefix = "".join("    " if parent_last else vertical for parent_last in parent_prefixes)
            marker = " (*)" if len(graph.dependencies[child]) > 1 else ""
            repeated = child in expanded
            repeated_marker = " [already shown]" if repeated else ""
            lines.append(f"{prefix}{branches[last]}{child}{marker}{repeated_marker}")
            if repeated:
                repeated_target = True
            else:
                expanded.add(child)
                grandchildren = graph.direct_dependents(child)
                child_prefixes = (*parent_prefixes, last)
                stack.extend(
                    (grandchild, child_prefixes, index == len(grandchildren) - 1)
                    for index, grandchild in reversed(tuple(enumerate(grandchildren)))
                )

    roots = graph.roots()
    for root_index, root in enumerate(roots):
        last = root_index == len(roots) - 1
        lines.append(f"{branches[last]}{root}")
        expanded.add(root)
        render_children(root, (last,))
    if any(len(dependencies) > 1 for dependencies in graph.dependencies.values()):
        lines.extend(["", "(*) target has more than one local dependency"])
    if repeated_target:
        lines.append("[already shown] incoming edge retained; repeated subtree omitted")
    return "\n".join(lines)
