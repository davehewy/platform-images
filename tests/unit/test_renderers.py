from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from platform_images.config import RepositoryConfig
from platform_images.discovery import discover_targets
from platform_images.errors import PlatformImagesError
from platform_images.graph import build_graph
from platform_images.models import BuildBackend, BuildMode, BuildPlan, RegistryTransport
from platform_images.planner import all_ci_plan, local_plan
from platform_images.renderers.github import (
    GITHUB_MATRIX_JOB_LIMIT,
    graph_layer_count,
    graph_parallel_width_bound,
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
    assert "--result-file image-results/curl.json" in command
    manifest = document["publish_image_manifest"]
    assert manifest.get("needs") is None
    assert "--expected-target curl" in manifest["script"][-1]
    assert manifest["artifacts"]["paths"] == ["image-build-manifest.json"]


def test_no_change_pipeline_has_executable_noop(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config, _graph = state(root)
    plan = BuildPlan(1, BuildMode.MERGE_REQUEST, "a", "b", (), ())
    document = yaml.safe_load(render_gitlab(plan, config, "registry.example.com"))
    assert document["stages"] == ["build", "manifest", "consume"]
    job = document["publish_image_manifest"]
    assert job["stage"] == "manifest"
    assert job["script"][0] == 'echo "No container images are affected."'
    assert "build-manifest --mode merge_request --commit-sha b" in job["script"][1]
    assert job["artifacts"]["paths"] == ["image-build-manifest.json"]


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
    assert document["stages"] == ["build", "manifest", "consume", "promote"]
    assert document["promote_main"].get("needs") is None
    assert len(document["promote_main"]["script"]) == 2
    assert all(":main" in command for command in document["promote_main"]["script"])


def test_gitlab_renderer_checks_the_complete_child_pipeline_job_budget(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config, graph = state(root)
    merge_request = all_ci_plan(
        graph,
        config,
        head_sha="abc",
        mode=BuildMode.MERGE_REQUEST,
        registry="registry.example.com",
        pipeline_id="123",
    )
    default_branch = all_ci_plan(
        graph,
        config,
        head_sha="abc",
        mode=BuildMode.DEFAULT_BRANCH,
        registry="registry.example.com",
        pipeline_id="123",
    )

    assert "publish_image_manifest" in yaml.safe_load(
        render_gitlab(merge_request, config, "registry.example.com", max_jobs=2)
    )
    with pytest.raises(
        PlatformImagesError,
        match=r"requires 3 jobs \(1 image builds, manifest, promotion\)",
    ):
        render_gitlab(default_branch, config, "registry.example.com", max_jobs=2)


def test_gitlab_renderer_rejects_an_invalid_job_limit(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config, _graph = state(root)
    no_changes = BuildPlan(1, BuildMode.MERGE_REQUEST, "a", "b", (), ())

    with pytest.raises(PlatformImagesError, match="must be at least 1"):
        render_gitlab(no_changes, config, "registry.example.com", max_jobs=0)


def test_gitlab_manifest_stage_avoids_a_wide_needs_fan_in(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({f"image-{index:03d}": "FROM alpine\n" for index in range(100)})
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

    manifest = document["publish_image_manifest"]
    assert manifest.get("needs") is None
    assert manifest["script"][0].count("--expected-target") == 100
    assert document["promote_main"].get("needs") is None


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
    assert output[5] == "has_targets=true"


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


def test_github_wide_layers_are_sharded_at_the_provider_limit(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    image_count = GITHUB_MATRIX_JOB_LIMIT + 1
    root = repository_factory(
        {f"image-{index:03d}": "FROM alpine\n" for index in range(image_count)}
    )
    config, graph = state(root)
    plan = all_ci_plan(
        graph,
        config,
        head_sha="abc",
        mode=BuildMode.MERGE_REQUEST,
        registry="registry.example.com",
        pipeline_id="123",
    )

    assert graph_parallel_width_bound(graph) == image_count
    output = dict(line.split("=", 1) for line in render_github_outputs(plan, 1, 2).splitlines())
    first = yaml.safe_load(output["layer_0_shard_0"])["include"]
    second = yaml.safe_load(output["layer_0_shard_1"])["include"]
    assert len(first) == GITHUB_MATRIX_JOB_LIMIT
    assert len(second) == 1
    assert {entry["target"] for entry in first + second} == set(graph.targets)

    document = yaml.safe_load(render_github_workflow(graph, config))
    jobs = document["jobs"]
    first_job = jobs["image_layer_0_shard_0"]
    second_job = jobs["image_layer_0_shard_1"]
    assert first_job["needs"] == ["plan"]
    assert second_job["needs"] == ["plan"]
    assert first_job["strategy"]["matrix"] == (
        "${{ fromJSON(needs.plan.outputs.layer_0_shard_0) }}"
    )
    assert jobs["manifest"]["needs"] == [
        "plan",
        "image_layer_0_shard_0",
        "image_layer_0_shard_1",
    ]
    matrix_command = next(
        step["run"]
        for step in jobs["plan"]["steps"]
        if step.get("name") == "Create dependency-safe build matrices"
    )
    assert "--max-shards 2" in matrix_command


def test_github_matrix_rejects_a_wide_plan_when_the_workflow_has_too_few_shards(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory(
        {f"image-{index:03d}": "FROM alpine\n" for index in range(GITHUB_MATRIX_JOB_LIMIT + 1)}
    )
    config, graph = state(root)
    plan = all_ci_plan(
        graph,
        config,
        head_sha="abc",
        mode=BuildMode.MERGE_REQUEST,
        registry="registry.example.com",
        pipeline_id="123",
    )

    with pytest.raises(PlatformImagesError, match="requires 2 GitHub matrix shards"):
        render_github_outputs(plan, 1)


def test_github_shard_bound_does_not_multiply_deep_narrow_graphs(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory(
        {
            "root": "FROM alpine\n",
            "middle": "FROM root\n",
            "leaf": "FROM middle\n",
        }
    )
    config, graph = state(root)

    assert graph_parallel_width_bound(graph) == 1
    jobs = yaml.safe_load(render_github_workflow(graph, config))["jobs"]
    assert {job_id for job_id in jobs if job_id.startswith("image_layer_")} == {
        "image_layer_0",
        "image_layer_1",
        "image_layer_2",
    }


def test_github_shard_bound_covers_antichains_spanning_static_depths(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory(
        {
            "a": "FROM alpine\n",
            "b": "FROM alpine\n",
            "c": "FROM a\n",
            "d": "FROM a\n",
            "e": "FROM d\n",
        }
    )
    _config, graph = state(root)

    # b, c, and e are mutually independent even though they occupy three different longest-path
    # depths. Bounding only the widest static depth (two) would therefore be unsafe for a partial
    # affected plan that selects those three leaves.
    assert graph_layer_count(graph) == 3
    assert graph_parallel_width_bound(graph) == 3


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
    build_command = next(
        step["run"]
        for step in jobs["image_layer_1"]["steps"]
        if step.get("name") == "Build and push exact planned target"
    )
    assert "build-plan-target" in build_command
    assert "--builder docker --registry-transport docker" in build_command
    assert "--result-file" in build_command
    assert jobs["manifest"]["needs"] == ["plan", "image_layer_1"]
    manifest_command = next(
        step["run"]
        for step in jobs["manifest"]["steps"]
        if step.get("name") == "Create verified commit-to-image manifest"
    )
    assert "build-manifest image-results --plan" in manifest_command
    assert jobs["promote"]["needs"] == ["plan", "manifest"]
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
            builder=BuildBackend.PODMAN,
            registry_transport=RegistryTransport.PODMAN,
            aws_auth="ambient",
            runner="self-hosted",
        )
    )
    layer = document["jobs"]["image_layer_0"]
    assert layer["runs-on"] == "self-hosted"
    assert "id-token" not in layer["permissions"]
    assert any(step.get("name") == "Install containers-storage tooling" for step in layer["steps"])
    assert any(
        "--builder podman --registry-transport podman" in step.get("run", "")
        for step in layer["steps"]
    )


def test_generated_github_workflow_uses_oci_registry_secrets_without_aws(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'stable_tag = "main"',
            'stable_tag = "main"\nprovider = "oci"\nauthentication = "credentials"\n'
            'username_environment_variable = "NEXUS_USERNAME"\n'
            'password_environment_variable = "NEXUS_PASSWORD"',
        ),
        encoding="utf-8",
    )
    config, graph = state(root)

    document = yaml.safe_load(render_github_workflow(graph, config))
    plan = document["jobs"]["plan"]
    layer = document["jobs"]["image_layer_0"]

    assert "NEXUS_USERNAME" not in document["jobs"]["validate"]["env"]
    assert "NEXUS_PASSWORD" not in document["jobs"]["validate"]["env"]
    assert "NEXUS_PASSWORD" not in document["jobs"]["manifest"]["env"]
    assert plan["env"]["NEXUS_USERNAME"] == "${{ secrets.NEXUS_USERNAME }}"
    assert plan["env"]["NEXUS_PASSWORD"] == "${{ secrets.NEXUS_PASSWORD }}"
    assert document["jobs"]["promote"]["env"]["NEXUS_PASSWORD"] == ("${{ secrets.NEXUS_PASSWORD }}")
    assert "id-token" not in plan["permissions"]
    assert plan["permissions"] == {"contents": "read"}
    assert not any("AWS" in step.get("name", "") for step in plan["steps"])
    assert any(step.get("name") == "Authenticate registry transport" for step in layer["steps"])


def test_generated_github_workflow_can_use_native_ghcr_identity(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'stable_tag = "main"',
            'stable_tag = "main"\nprovider = "oci"\nauthentication = "credentials"\n'
            'username_environment_variable = "GITHUB_ACTOR"\n'
            'password_environment_variable = "GITHUB_TOKEN"',
        ),
        encoding="utf-8",
    )
    config, graph = state(root)

    document = yaml.safe_load(render_github_workflow(graph, config))
    plan = document["jobs"]["plan"]

    assert plan["env"]["GITHUB_ACTOR"] == "${{ github.actor }}"
    assert plan["env"]["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert plan["permissions"] == {"contents": "read", "packages": "write"}
    assert "GITHUB_TOKEN" not in document["jobs"]["validate"]["env"]


@pytest.mark.parametrize(
    ("builder", "transport", "verification"),
    [
        (BuildBackend.BUILDAH, RegistryTransport.BUILDAH, "buildah version"),
        (BuildBackend.NERDCTL, RegistryTransport.NERDCTL, "buildctl --version"),
    ],
)
def test_generated_github_workflow_supports_additional_execution_pairs(
    repository_factory: Callable[[dict[str, str]], Path],
    builder: BuildBackend,
    transport: RegistryTransport,
    verification: str,
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config, graph = state(root)

    document = yaml.safe_load(
        render_github_workflow(
            graph,
            config,
            builder=builder,
            registry_transport=transport,
            aws_auth="ambient",
            runner="self-hosted",
        )
    )
    layer = document["jobs"]["image_layer_0"]
    scripts = "\n".join(str(step.get("run", "")) for step in layer["steps"])

    assert verification in scripts
    assert f"--builder {builder.value}" in scripts
    assert f"--registry-transport {transport.value}" in scripts


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
    assert template["variables"]["PLATFORM_IMAGES_RUNNER_TAG"] == "podman"
    assert template[".image-build"]["tags"] == ["$PLATFORM_IMAGES_RUNNER_TAG"]
    assert template[".image-build"]["before_script"][-1] == ("platform images registry-login")
    assert not any(key.startswith("image_") for key in parent)
