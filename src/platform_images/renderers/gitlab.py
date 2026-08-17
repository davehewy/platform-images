from __future__ import annotations

import shlex

import yaml

from platform_images.config import RepositoryConfig
from platform_images.errors import PlatformImagesError
from platform_images.models import BuildMode, BuildPlan
from platform_images.references import ReferencePolicy

GITLAB_DEFAULT_JOB_LIMIT = 500


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


def render_gitlab(
    plan: BuildPlan,
    config: RepositoryConfig,
    registry: str,
    *,
    max_jobs: int = GITLAB_DEFAULT_JOB_LIMIT,
) -> str:
    if max_jobs < 1:
        raise PlatformImagesError("--gitlab-max-jobs must be at least 1")
    promotion_jobs = 1 if plan.mode is BuildMode.DEFAULT_BRANCH and plan.targets else 0
    required_jobs = len(plan.targets) + 1 + promotion_jobs
    if required_jobs > max_jobs:
        promotion_note = ", promotion" if promotion_jobs else ""
        raise PlatformImagesError(
            f"generated GitLab child pipeline requires {required_jobs} jobs "
            f"({len(plan.targets)} image builds, manifest{promotion_note}), exceeding "
            f"--gitlab-max-jobs {max_jobs}; set that option to the limit configured for your "
            "GitLab tier or instance, or reduce the number of targets in one pipeline"
        )
    manifest_command = [
        "platform",
        "images",
        "build-manifest",
        *(["image-results"] if plan.targets else []),
        "--mode",
        plan.mode.value,
        "--commit-sha",
        plan.head_sha,
    ]
    if plan.base_sha is not None:
        manifest_command.extend(["--base-sha", plan.base_sha])
    for target in plan.targets:
        manifest_command.extend(["--expected-target", target.name])
    manifest_command.extend(["--output", "image-build-manifest.json"])

    document: dict[str, object] = {"stages": ["build", "manifest", "consume"]}

    for target in plan.targets:
        command = [
            "platform",
            "images",
            "ci-build",
            target.name,
            "--output-ref",
            target.output_ref,
            "--result-file",
            f"image-results/{target.name}.json",
        ]
        for dependency, reference in sorted(target.input_refs.items()):
            command.extend(["--input-ref", f"{dependency}={reference}"])
        job: dict[str, object] = {
            "extends": ".image-build",
            "script": [shlex.join(command)],
            "artifacts": {"paths": [f"image-results/{target.name}.json"]},
        }
        if target.needs:
            job["needs"] = [job_name(dependency) for dependency in target.needs]
        document[job_name(target.name)] = job

    document["publish_image_manifest"] = {
        "extends": ".image-build",
        "stage": "manifest",
        "script": [
            *(['echo "No container images are affected."'] if not plan.targets else []),
            shlex.join(manifest_command),
        ],
        "artifacts": {
            "name": "image-build-manifest-$CI_COMMIT_SHA",
            "paths": ["image-build-manifest.json"],
        },
    }

    if plan.mode is BuildMode.DEFAULT_BRANCH and plan.targets:
        document["stages"] = ["build", "manifest", "consume", "promote"]
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
            "script": scripts,
        }
    return yaml.safe_dump(document, sort_keys=False)
