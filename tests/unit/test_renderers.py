from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from platform_images.config import RepositoryConfig
from platform_images.discovery import discover_targets
from platform_images.errors import PlatformImagesError
from platform_images.graph import build_graph
from platform_images.models import BuildEngine, BuildMode, BuildPlan
from platform_images.planner import all_ci_plan, local_plan
from platform_images.renderers.github import (
    graph_layer_count,
    plan_layers,
    render_github_outputs,
    render_github_workflow,
)
from platform_images.renderers.gitlab import job_name, render_gitlab
from platform_images.renderers.plan_json import load_plan_json, plan_data, render_plan_json


def state(root: Path):
    config = RepositoryConfig.load(root)
    return config, build_graph(discover_targets(root), config)


def test_plan_json_is_authoritative_and_stable(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    config, graph = state(root)
    data = plan_data(local_plan(graph, config, frozenset({"curl"})))
    assert data["schema_version"] == 1
    assert data["mode"] == "local"
    assert [target["name"] for target in data["targets"]] == ["base", "curl"]
    assert data["targets"][1]["needs"] == ["base"]

    loaded = load_plan_json(render_plan_json(local_plan(graph, config, frozenset({"curl"}))))
    assert loaded == local_plan(graph, config, frozenset({"curl"}))


def test_gitlab_jobs_have_only_direct_needs_and_exact_inputs(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"api": "FROM alpine\n", "base": "FROM api\n", "curl": "FROM base\n"})
    config, graph = state(root)
    plan = all_ci_plan(
        graph,
        config,
        head_sha="abc",
        mode=BuildMode.MERGE_REQUEST,
        registry="registry.example.com",
        pipeline_id="123",
    )
    document = yaml.safe_load(render_gitlab(plan, config, "registry.example.com"))
    assert document["image_api"].get("needs") is None
    assert document["image_base"]["needs"] == ["image_api"]
    assert document["image_curl"]["needs"] == ["image_base"]
    command = document["image_curl"]["script"][0]
    assert "--input-ref base=registry.example.com/platform-images/base:ci-123-abc" in command


def test_no_change_pipeline_has_executable_noop(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config, _graph = state(root)
    plan = BuildPlan(1, BuildMode.MERGE_REQUEST, "a", "b", (), ())
    document = yaml.safe_load(render_gitlab(plan, config, "registry.example.com"))
    assert document == {
        "stages": ["build"],
        "no_image_changes": {
            "stage": "build",
            "script": ['echo "No container images are affected."'],
        },
    }


def test_default_branch_has_one_graph_gated_promotion_job(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    config, graph = state(root)
    plan = all_ci_plan(
        graph,
        config,
        head_sha="abc",
        mode=BuildMode.DEFAULT_BRANCH,
        registry="registry.example.com",
        pipeline_id="123",
    )
    document = yaml.safe_load(render_gitlab(plan, config, "registry.example.com"))
    assert document["stages"] == ["build", "promote"]
    assert document["promote_main"]["needs"] == ["image_base", "image_curl"]
    assert len(document["promote_main"]["script"]) == 2
    assert all(":main" in command for command in document["promote_main"]["script"])


def test_gitlab_job_names_cannot_collide_for_valid_separators() -> None:
    names = {job_name(name) for name in ("a-b", "a_b", "a.b", "a-hyphen-b")}
    assert len(names) == 4


def test_github_matrices_parallelize_only_dependency_safe_waves(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory(
        {
            "api": "FROM alpine\n",
            "base": "FROM api\n",
            "curl": "FROM base\n",
            "jq": "FROM base\n",
            "standalone": "FROM busybox\n",
        }
    )
    config, graph = state(root)
    plan = all_ci_plan(
        graph,
        config,
        head_sha="abc",
        mode=BuildMode.DEFAULT_BRANCH,
        registry="registry.example.com",
        pipeline_id="123",
    )

    assert graph_layer_count(graph) == 3
    assert plan_layers(plan) == (("api", "standalone"), ("base",), ("curl", "jq"))
    output = render_github_outputs(plan, 4).splitlines()
    assert output[0] == "mode=default_branch"
    assert output[1] == 'layer_0={"include":[{"target":"api"},{"target":"standalone"}]}'
    assert output[3] == 'layer_2={"include":[{"target":"curl"},{"target":"jq"}]}'
    assert output[4] == 'layer_3={"include":[{"target":""}]}'


def test_github_matrix_fails_when_checked_in_workflow_is_stale(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"api": "FROM alpine\n", "base": "FROM api\n"})
    config, graph = state(root)
    plan = all_ci_plan(
        graph,
        config,
        head_sha="abc",
        mode=BuildMode.MERGE_REQUEST,
        registry="registry.example.com",
        pipeline_id="123",
    )
    with pytest.raises(PlatformImagesError, match="regenerate"):
        render_github_outputs(plan, 1)


def test_generated_github_workflow_has_static_waves_dynamic_matrices_and_gated_promotion(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    config, graph = state(root)

    rendered = render_github_workflow(graph, config)
    document = yaml.safe_load(rendered)
    jobs = document["jobs"]

    assert "&id" not in rendered
    assert document["on"]["push"]["branches"] == ["main"]
    assert jobs["plan"]["needs"] == "validate"
    assert "head.repo.full_name" in jobs["plan"]["if"]
    assert jobs["plan"]["env"]["CI_COMMIT_BEFORE_SHA"] == "${{ github.event.before }}"
    assert "CI_MERGE_REQUEST_DIFF_BASE_SHA" not in jobs["plan"]["env"]
    assert jobs["image_layer_0"]["needs"] == ["plan"]
    assert jobs["image_layer_1"]["needs"] == ["plan", "image_layer_0"]
    assert jobs["image_layer_1"]["strategy"]["matrix"] == (
        "${{ fromJSON(needs.plan.outputs.layer_1) }}"
    )
    build_command = jobs["image_layer_1"]["steps"][-1]["run"]
    assert "build-plan-target" in build_command
    assert "--engine docker" in build_command
    assert jobs["promote"]["needs"] == ["plan", "image_layer_1"]
    assert "default_branch" in jobs["promote"]["if"]
    assert "id-token" in jobs["plan"]["permissions"]

    action_refs = [step["uses"] for job in jobs.values() for step in job["steps"] if "uses" in step]
    assert action_refs
    assert all(len(reference.rsplit("@", 1)[1]) == 40 for reference in action_refs)


def test_generated_github_workflow_supports_podman_and_ambient_credentials(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config, graph = state(root)
    document = yaml.safe_load(
        render_github_workflow(
            graph,
            config,
            engine=BuildEngine.PODMAN,
            aws_auth="ambient",
            runner="self-hosted",
        )
    )
    layer = document["jobs"]["image_layer_0"]
    assert layer["runs-on"] == "self-hosted"
    assert "id-token" not in layer["permissions"]
    assert any(step.get("name") == "Install Podman" for step in layer["steps"])
    assert any("--engine podman" in step.get("run", "") for step in layer["steps"])


def test_checked_in_parent_and_child_template_match_dynamic_contract() -> None:
    root = Path(__file__).parents[2]
    parent = yaml.safe_load((root / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    template = yaml.safe_load(
        (root / ".gitlab" / "image-build-jobs.yml").read_text(encoding="utf-8")
    )
    trigger = parent["run-image-pipeline"]["trigger"]
    assert trigger["strategy"] == "mirror"
    assert trigger["include"] == [
        {"local": ".gitlab/image-build-jobs.yml"},
        {"artifact": "generated-images.yml", "job": "generate-image-pipeline"},
    ]
    assert parent["generate-image-pipeline"]["variables"]["GIT_DEPTH"] == "0"
    scripts = parent["generate-image-pipeline"]["script"]
    assert sum("platform images plan --ci" in command for command in scripts) == 1
    assert (
        "platform images render-plan image-plan.json --format gitlab > generated-images.yml"
        in scripts
    )
    assert template[".image-build"]["tags"] == ["podman"]
    assert template[".image-build"]["before_script"][-1] == ("platform images registry-login")
    assert not any(key.startswith("image_") for key in parent)
