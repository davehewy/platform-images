from __future__ import annotations

import json
import re
from collections.abc import Mapping

import yaml

from platform_images import __version__
from platform_images.backends import validate_execution_pair
from platform_images.config import RepositoryConfig
from platform_images.errors import PlatformImagesError
from platform_images.graph import ImageGraph
from platform_images.models import BuildBackend, BuildPlan, RegistryTransport

CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"  # v7.0.1
UPLOAD_ARTIFACT = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"  # v7.0.1
DOWNLOAD_ARTIFACT = "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"  # v8.0.1
AWS_CREDENTIALS = (
    "aws-actions/configure-aws-credentials@e6de054238d6b7531b4efff3b6587d9aade6a06c"  # v6.2.3
)


class _WorkflowDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def plan_layers(plan: BuildPlan) -> tuple[tuple[str, ...], ...]:
    """Group a topological plan into dependency-safe parallel build waves."""
    depths: dict[str, int] = {}
    layers: list[list[str]] = []
    for target in plan.targets:
        depth = (
            0 if not target.needs else max(depths[dependency] for dependency in target.needs) + 1
        )
        depths[target.name] = depth
        while len(layers) <= depth:
            layers.append([])
        layers[depth].append(target.name)
    return tuple(tuple(names) for names in layers)


def graph_layer_count(graph: ImageGraph) -> int:
    depths: dict[str, int] = {}
    for name in graph.topological_order():
        dependencies = graph.direct_dependencies(name)
        depths[name] = (
            0 if not dependencies else max(depths[dependency] for dependency in dependencies) + 1
        )
    return max(depths.values(), default=0) + 1


def render_github_outputs(plan: BuildPlan, max_layers: int) -> str:
    if max_layers < 1:
        raise PlatformImagesError("--max-layers must be at least 1")
    layers = plan_layers(plan)
    if len(layers) > max_layers:
        raise PlatformImagesError(
            f"plan requires {len(layers)} GitHub build layers but the workflow provides "
            f"{max_layers}; regenerate it with 'platform images generate-workflow github'"
        )
    lines = [f"mode={plan.mode.value}"]
    for index in range(max_layers):
        names = layers[index] if index < len(layers) else ()
        # GitHub rejects an empty dynamic matrix, so a harmless sentinel keeps every static wave
        # successful when a change plan has no work at that depth.
        include = [{"target": name} for name in names] or [{"target": ""}]
        value = json.dumps({"include": include}, separators=(",", ":"))
        lines.append(f"layer_{index}={value}")
    return "\n".join(lines)


def _install_step() -> dict[str, object]:
    return {
        "name": "Install platform-images",
        "env": {"PLATFORM_IMAGES_VERSION": __version__},
        "run": (
            'curl -fsSL "https://raw.githubusercontent.com/davehewy/platform-images/'
            'v${PLATFORM_IMAGES_VERSION}/scripts/install.sh" '
            '-o "$RUNNER_TEMP/install-platform-images.sh"\n'
            'PLATFORM_IMAGES_INSTALL_DIR="$RUNNER_TEMP/platform-images-bin" '
            'sh "$RUNNER_TEMP/install-platform-images.sh"\n'
            'echo "$RUNNER_TEMP/platform-images-bin" >> "$GITHUB_PATH"'
        ),
    }


def _checkout_step(*, full_history: bool = False) -> dict[str, object]:
    step: dict[str, object] = {
        "name": "Check out source",
        "uses": CHECKOUT,
        "with": {"ref": "${{ env.CI_COMMIT_SHA }}"},
    }
    if full_history:
        step["with"] = {"ref": "${{ env.CI_COMMIT_SHA }}", "fetch-depth": 0}
    return step


def _aws_step(role_variable: str, region_variable: str) -> dict[str, object]:
    return {
        "name": "Configure short-lived AWS credentials",
        "uses": AWS_CREDENTIALS,
        "with": {
            "role-to-assume": f"${{{{ vars.{role_variable} }}}}",
            "aws-region": f"${{{{ vars.{region_variable} }}}}",
        },
    }


def _artifact_download_step() -> dict[str, object]:
    return {
        "name": "Download authoritative image plan",
        "uses": DOWNLOAD_ARTIFACT,
        "with": {
            "name": "image-plan-${{ github.run_id }}-${{ github.run_attempt }}",
            "path": ".platform-images",
        },
    }


def _runtime_steps(builder: BuildBackend, transport: RegistryTransport) -> list[dict[str, object]]:
    required = {builder.value, transport.value}
    if required == {BuildBackend.DOCKER.value}:
        return [{"name": "Verify Docker Buildx", "run": "docker buildx version"}]
    packages = sorted(required & {BuildBackend.PODMAN.value, BuildBackend.BUILDAH.value})
    steps: list[dict[str, object]] = []
    if packages:
        steps.append(
            {
                "name": "Install containers-storage tooling",
                "run": ("sudo apt-get update\nsudo apt-get install --yes " + " ".join(packages)),
            }
        )
    for executable in sorted(required):
        if executable == BuildBackend.DOCKER.value:
            steps.append({"name": "Verify Docker Buildx", "run": "docker buildx version"})
        elif executable == BuildBackend.NERDCTL.value:
            steps.append(
                {
                    "name": "Verify nerdctl, containerd, and BuildKit",
                    "run": "nerdctl version\nnerdctl info\nbuildctl --version",
                }
            )
        else:
            steps.append(
                {
                    "name": f"Verify {executable}",
                    "run": f"{executable} version\n{executable} info",
                }
            )
    return steps


def _valid_variable(name: str, option: str) -> None:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
        raise PlatformImagesError(f"{option} must be a valid GitHub Actions variable name")


def render_github_workflow(
    graph: ImageGraph,
    config: RepositoryConfig,
    *,
    default_branch: str = "main",
    runner: str = "ubuntu-latest",
    builder: BuildBackend | None = None,
    registry_transport: RegistryTransport | None = None,
    engine: BuildBackend | None = None,
    aws_auth: str = "oidc",
    aws_role_variable: str = "AWS_ROLE_TO_ASSUME",
    aws_region_variable: str = "AWS_REGION",
) -> str:
    if builder is not None and engine is not None and builder is not engine:
        raise ValueError("builder and legacy engine select different backends")
    explicitly_selected = builder or engine
    builder = builder or engine or BuildBackend.DOCKER
    if registry_transport is None:
        registry_transport = (
            RegistryTransport(explicitly_selected.value)
            if explicitly_selected is not None
            else RegistryTransport.DOCKER
        )
    validate_execution_pair(builder, registry_transport)
    if not default_branch or not runner:
        raise PlatformImagesError("default branch and runner must not be empty")
    if aws_auth not in {"oidc", "ambient"}:
        raise PlatformImagesError("AWS authentication must be 'oidc' or 'ambient'")
    _valid_variable(aws_role_variable, "--aws-role-variable")
    _valid_variable(aws_region_variable, "--aws-region-variable")
    layer_count = graph_layer_count(graph)
    registry_variable = config.registry.registry_environment_variable
    source_env: dict[str, str] = {
        registry_variable: f"${{{{ vars.{registry_variable} }}}}",
        "CI_COMMIT_SHA": "${{ github.event.pull_request.head.sha || github.sha }}",
        "CI_COMMIT_BEFORE_SHA": "${{ github.event.before }}",
        "CI_COMMIT_BRANCH": "${{ github.head_ref || github.ref_name }}",
        "CI_DEFAULT_BRANCH": default_branch,
        "CI_PIPELINE_ID": "${{ github.run_id }}",
        "CI_PROJECT_URL": "${{ github.server_url }}/${{ github.repository }}",
        "CI_MERGE_REQUEST_IID": "${{ github.event.pull_request.number }}",
    }
    permissions: dict[str, str] = {"contents": "read"}
    if aws_auth == "oidc":
        permissions["id-token"] = "write"
    auth_steps = [_aws_step(aws_role_variable, aws_region_variable)] if aws_auth == "oidc" else []

    jobs: dict[str, object] = {
        "validate": {
            "name": "Validate image graph",
            "runs-on": runner,
            "timeout-minutes": 10,
            "env": source_env,
            "steps": [
                _checkout_step(),
                _install_step(),
                {"name": "Validate repository", "run": "platform images validate"},
            ],
        },
        "plan": {
            "name": "Plan affected images",
            "needs": "validate",
            "if": (
                "github.event_name != 'pull_request' || "
                "github.event.pull_request.head.repo.full_name == github.repository"
            ),
            "runs-on": runner,
            "timeout-minutes": 10,
            "permissions": permissions,
            "env": {
                **source_env,
                "REBUILD_ALL": "${{ inputs.rebuild_all || 'false' }}",
            },
            "outputs": {
                "mode": "${{ steps.matrix.outputs.mode }}",
                **{
                    f"layer_{index}": f"${{{{ steps.matrix.outputs.layer_{index} }}}}"
                    for index in range(layer_count)
                },
            },
            "steps": [
                _checkout_step(full_history=True),
                _install_step(),
                *auth_steps,
                {
                    "name": "Calculate one authoritative plan",
                    "run": (
                        'if [ "$REBUILD_ALL" = "true" ]; then\n'
                        "  platform images plan --ci --all --format json > image-plan.json\n"
                        "else\n"
                        "  platform images plan --ci --format json > image-plan.json\n"
                        "fi"
                    ),
                },
                {
                    "name": "Create dependency-safe build matrices",
                    "id": "matrix",
                    "run": (
                        f"platform images github-matrix image-plan.json --max-layers "
                        f'{layer_count} >> "$GITHUB_OUTPUT"'
                    ),
                },
                {
                    "name": "Upload authoritative image plan",
                    "uses": UPLOAD_ARTIFACT,
                    "with": {
                        "name": "image-plan-${{ github.run_id }}-${{ github.run_attempt }}",
                        "path": "image-plan.json",
                        "if-no-files-found": "error",
                    },
                },
            ],
        },
    }

    previous = "plan"
    for index in range(layer_count):
        job_id = f"image_layer_{index}"
        jobs[job_id] = {
            "name": f"Build layer {index + 1} / ${{{{ matrix.target || 'no changes' }}}}",
            "needs": ["plan"] if index == 0 else ["plan", previous],
            "runs-on": runner,
            "timeout-minutes": 45,
            "permissions": permissions,
            "env": source_env,
            "strategy": {
                "fail-fast": False,
                "matrix": f"${{{{ fromJSON(needs.plan.outputs.layer_{index}) }}}}",
            },
            "steps": [
                {
                    "name": "No image changes in this layer",
                    "if": "matrix.target == ''",
                    "run": 'echo "No affected images in this dependency layer."',
                },
                {**_checkout_step(), "if": "matrix.target != ''"},
                {**_install_step(), "if": "matrix.target != ''"},
                *[{**step, "if": "matrix.target != ''"} for step in auth_steps],
                *[
                    {**step, "if": "matrix.target != ''"}
                    for step in _runtime_steps(builder, registry_transport)
                ],
                {**_artifact_download_step(), "if": "matrix.target != ''"},
                {
                    "name": "Authenticate registry transport to ECR",
                    "if": "matrix.target != ''",
                    "run": (
                        "platform images registry-login --registry-transport "
                        f"{registry_transport.value}"
                    ),
                },
                {
                    "name": "Build and push exact planned target",
                    "if": "matrix.target != ''",
                    "run": (
                        "platform images build-plan-target .platform-images/image-plan.json "
                        f'"${{{{ matrix.target }}}}" --builder {builder.value} '
                        f"--registry-transport {registry_transport.value}"
                    ),
                },
            ],
        }
        previous = job_id

    jobs["promote"] = {
        "name": f"Promote {default_branch} image set",
        "needs": ["plan", previous],
        "if": (
            "!cancelled() && needs.plan.result == 'success' && "
            f"needs.{previous}.result == 'success' && "
            "needs.plan.outputs.mode == 'default_branch'"
        ),
        "runs-on": runner,
        "timeout-minutes": 20,
        "permissions": permissions,
        "env": source_env,
        "steps": [
            _checkout_step(),
            _install_step(),
            *auth_steps,
            *_runtime_steps(builder, registry_transport),
            _artifact_download_step(),
            {
                "name": "Authenticate registry transport to ECR",
                "run": (
                    "platform images registry-login --registry-transport "
                    f"{registry_transport.value}"
                ),
            },
            {
                "name": "Promote only after every affected image succeeds",
                "run": (
                    "platform images promote-plan .platform-images/image-plan.json "
                    f"--registry-transport {registry_transport.value}"
                ),
            },
        ],
    }

    document: Mapping[str, object] = {
        "name": "Container images",
        "on": {
            "push": {"branches": [default_branch]},
            "pull_request": {},
            "workflow_dispatch": {
                "inputs": {
                    "rebuild_all": {
                        "description": "Rebuild every image instead of detecting changes",
                        "required": False,
                        "default": False,
                        "type": "boolean",
                    }
                }
            },
        },
        "permissions": {"contents": "read"},
        "concurrency": {
            "group": "platform-images-${{ github.workflow }}-${{ github.ref }}",
            "cancel-in-progress": True,
        },
        "jobs": jobs,
    }
    rendered = yaml.dump(document, Dumper=_WorkflowDumper, sort_keys=False, width=1000)
    return (
        "# Generated by platform images generate-workflow github.\n"
        "# Regenerate after changing the image dependency graph.\n" + rendered
    )
