from __future__ import annotations

import shlex

import yaml

from platform_images.config import RepositoryConfig
from platform_images.models import BuildMode, BuildPlan
from platform_images.references import ReferencePolicy


def job_name(target: str) -> str:
    escaped = "".join(
        {
            "-": "_hyphen_",
            ".": "_dot_",
            "_": "_underscore_",
        }.get(character, character)
        for character in target
    )
    return "image_" + escaped


def render_gitlab(plan: BuildPlan, config: RepositoryConfig, registry: str) -> str:
    document: dict[str, object] = {"stages": ["build"]}
    if not plan.targets:
        document["no_image_changes"] = {
            "stage": "build",
            "script": ['echo "No container images are affected."'],
        }
        return yaml.safe_dump(document, sort_keys=False)

    for target in plan.targets:
        command = [
            "platform",
            "images",
            "ci-build",
            target.name,
            "--output-ref",
            target.output_ref,
        ]
        for dependency, reference in sorted(target.input_refs.items()):
            command.extend(["--input-ref", f"{dependency}={reference}"])
        job: dict[str, object] = {
            "extends": ".image-build",
            "script": [shlex.join(command)],
        }
        if target.needs:
            job["needs"] = [job_name(dependency) for dependency in target.needs]
        document[job_name(target.name)] = job

    if plan.mode is BuildMode.DEFAULT_BRANCH:
        document["stages"] = ["build", "promote"]
        policy = ReferencePolicy(config, registry)
        scripts: list[str] = []
        for target in plan.targets:
            scripts.append(
                shlex.join(
                    [
                        "platform",
                        "images",
                        "promote",
                        "--source",
                        target.output_ref,
                        "--destination",
                        policy.stable(target.name),
                    ]
                )
            )
        document["promote_main"] = {
            "extends": ".image-build",
            "stage": "promote",
            "needs": [job_name(target.name) for target in plan.targets],
            "script": scripts,
        }
    return yaml.safe_dump(document, sort_keys=False)
