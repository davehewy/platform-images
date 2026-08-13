from __future__ import annotations

import json
from typing import Any

from platform_images.errors import PlatformImagesError
from platform_images.models import BuildMode, BuildPlan, BuildPlanTarget


def plan_data(plan: BuildPlan) -> dict[str, object]:
    return {
        "schema_version": plan.schema_version,
        "mode": plan.mode.value,
        "base_sha": plan.base_sha,
        "head_sha": plan.head_sha,
        "targets": [
            {
                "name": target.name,
                "reasons": list(target.reasons),
                "dependencies": list(target.dependencies),
                "needs": list(target.needs),
                "dockerfile": target.dockerfile,
                "context": target.context,
                "output_ref": target.output_ref,
                "input_refs": dict(sorted(target.input_refs.items())),
                "push": target.push,
            }
            for target in plan.targets
        ],
        "removed_targets": list(plan.removed_targets),
    }


def render_plan_json(plan: BuildPlan) -> str:
    return json.dumps(plan_data(plan), indent=2, sort_keys=False)


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PlatformImagesError(f"plan field {field!r} must be an array of strings")
    return tuple(value)


def load_plan_json(text: str) -> BuildPlan:
    """Load the persisted plan using a deliberately strict, versioned schema."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlatformImagesError(f"invalid plan JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PlatformImagesError("plan JSON must be an object")
    if data.get("schema_version") != 1:
        raise PlatformImagesError(
            f"unsupported plan schema_version: {data.get('schema_version')!r}"
        )
    try:
        mode = BuildMode(data["mode"])
        base_sha = data["base_sha"]
        head_sha = data["head_sha"]
        raw_targets = data["targets"]
        removed_targets = _string_list(data["removed_targets"], "removed_targets")
    except (KeyError, ValueError) as exc:
        raise PlatformImagesError(f"invalid plan: {exc}") from exc
    if base_sha is not None and not isinstance(base_sha, str):
        raise PlatformImagesError("plan field 'base_sha' must be a string or null")
    if not isinstance(head_sha, str) or not head_sha:
        raise PlatformImagesError("plan field 'head_sha' must be a non-empty string")
    if not isinstance(raw_targets, list):
        raise PlatformImagesError("plan field 'targets' must be an array")

    targets: list[BuildPlanTarget] = []
    for index, raw in enumerate(raw_targets):
        field = f"targets[{index}]"
        if not isinstance(raw, dict):
            raise PlatformImagesError(f"plan field {field!r} must be an object")
        try:
            name = raw["name"]
            dockerfile = raw["dockerfile"]
            context = raw["context"]
            output_ref = raw["output_ref"]
            push = raw["push"]
            input_refs = raw["input_refs"]
            reasons = _string_list(raw["reasons"], f"{field}.reasons")
            dependencies = _string_list(raw["dependencies"], f"{field}.dependencies")
            needs = _string_list(raw["needs"], f"{field}.needs")
        except KeyError as exc:
            raise PlatformImagesError(f"plan field {field!r} is missing {exc}") from exc
        string_fields = {
            "name": name,
            "dockerfile": dockerfile,
            "context": context,
            "output_ref": output_ref,
        }
        invalid = [key for key, value in string_fields.items() if not isinstance(value, str)]
        if invalid:
            raise PlatformImagesError(
                f"plan field {field!r} has non-string values: {', '.join(invalid)}"
            )
        if not isinstance(push, bool):
            raise PlatformImagesError(f"plan field '{field}.push' must be a boolean")
        if not isinstance(input_refs, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in input_refs.items()
        ):
            raise PlatformImagesError(
                f"plan field '{field}.input_refs' must be an object of strings"
            )
        targets.append(
            BuildPlanTarget(
                name,
                reasons,
                dependencies,
                needs,
                dockerfile,
                context,
                output_ref,
                dict(sorted(input_refs.items())),
                push,
            )
        )
    return BuildPlan(1, mode, base_sha, head_sha, tuple(targets), removed_targets)
