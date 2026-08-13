from __future__ import annotations

from platform_images.models import BuildPlan


def render_plan_text(plan: BuildPlan) -> str:
    if not plan.targets:
        return "No container images are affected."
    lines = [f"Build plan ({plan.mode.value}):"]
    for target in plan.targets:
        lines.append(f"  {target.name}")
        lines.append(f"    reasons: {', '.join(target.reasons)}")
        lines.append(f"    output: {target.output_ref}")
        if target.needs:
            lines.append(f"    needs: {', '.join(target.needs)}")
        for dependency, reference in sorted(target.input_refs.items()):
            lines.append(f"    input {dependency}: {reference}")
    if plan.removed_targets:
        lines.append("Removed targets: " + ", ".join(plan.removed_targets))
    return "\n".join(lines)
