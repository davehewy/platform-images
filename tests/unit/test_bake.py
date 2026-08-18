from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from conftest import git

from platform_images.cli import main
from platform_images.config import RepositoryConfig
from platform_images.discovery import discover_targets
from platform_images.graph import build_graph
from platform_images.models import BuildMode, BuildPlan, BuildPlanTarget
from platform_images.planner import all_ci_plan, local_plan
from platform_images.renderers.bake import bake_target_name, render_bake
from platform_images.renderers.plan_json import render_plan_json


def _state(root: Path):
    config = RepositoryConfig.load(root)
    return config, build_graph(discover_targets(root), config)


def _append_config(root: Path, text: str) -> None:
    path = root / "platform-images.toml"
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_bake_target_names_are_readable_valid_and_collision_safe() -> None:
    names = ("base", "base-image", "base_image", "base.image", "base_dot_image")
    encoded = tuple(bake_target_name(name) for name in names)

    assert len(encoded) == len(set(encoded))
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]+", name) for name in encoded)
    assert bake_target_name("base.image") == "image-base_dot_image"
    assert bake_target_name("base_dot_image") == "image-base__dot__image"


def test_bake_uses_target_contexts_for_every_local_reference_spelling(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory(
        {
            "base.image": "FROM alpine\n",
            "application": "FROM nexus.example.com/team/base.image:latest\n",
        }
    )
    _append_config(
        root,
        '\n[images."base.image"]\naliases = ["nexus.example.com/team/base.image"]\n',
    )
    config, graph = _state(root)
    plan = local_plan(graph, config, frozenset({"application"}))

    rendered = render_bake(plan, config, group_name="selected")

    assert rendered == render_bake(plan, config, group_name="selected")
    assert 'group "default" {' in rendered
    assert 'group "selected" {' in rendered
    assert 'target "image-base_dot_image" {' in rendered
    assert 'dockerfile = "Dockerfile"' in rendered
    assert '"base.image" = "target:image-base_dot_image"' in rendered
    assert '"nexus.example.com/team/base.image:latest" = "target:image-base_dot_image"' in rendered
    assert '"localhost/platform-images/base.image:dev"' in rendered


def test_bake_safely_inlines_only_argument_bound_dockerfiles(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory(
        {
            "base": "FROM alpine\n",
            "application": (
                "ARG SOURCE_REGISTRY\n"
                "FROM ${SOURCE_REGISTRY}/base:latest\n"
                "RUN echo ${UNRELATED_RUNTIME_VALUE}\n"
            ),
        }
    )
    _append_config(
        root,
        "\n[dockerfile.arguments]\n"
        'SOURCE_REGISTRY = "nexus.example.com/team"\n'
        "\n[images.base]\n"
        'aliases = ["nexus.example.com/team/base"]\n',
    )
    config, graph = _state(root)

    rendered = render_bake(
        local_plan(graph, config, frozenset({"application"})),
        config,
    )

    base_block, application_block = rendered.split('target "image-application" {', 1)
    assert "dockerfile-inline" not in base_block
    assert "dockerfile-inline = <<PLATFORM_IMAGES_DOCKERFILE" in application_block
    assert "FROM base" in application_block
    assert "RUN echo $${UNRELATED_RUNTIME_VALUE}" in application_block
    assert '"base" = "target:image-base"' in application_block
    assert "${SOURCE_REGISTRY}/base:latest" not in application_block
    assert '"SOURCE_REGISTRY" = "nexus.example.com/team"' in application_block


def test_bake_preserves_exact_ci_tags_inputs_and_identity_labels(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "application": "FROM base\n"})
    config, graph = _state(root)
    plan = all_ci_plan(
        graph,
        config,
        head_sha="0123456789abcdef",
        mode=BuildMode.DEFAULT_BRANCH,
        registry="registry.example.com",
        pipeline_id="42",
    )

    rendered = render_bake(
        plan,
        config,
        group_name="affected",
        source="https://example.com/team/repository",
    )

    assert 'group "affected" {' in rendered
    assert '"registry.example.com/platform-images/base:sha-0123456789abcdef"' in rendered
    assert '"org.opencontainers.image.revision" = "0123456789abcdef"' in rendered
    assert '"org.opencontainers.image.source" = "https://example.com/team/repository"' in rendered
    assert '"io.platform-images.target" = "application"' in rendered
    assert '"base" = "target:image-base"' in rendered
    assert "output =" not in rendered


def test_bake_pulls_an_unselected_local_parent_by_exact_planned_reference(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "application": "FROM base\n"})
    config, graph = _state(root)
    reference = "registry.example.com/platform-images/base@sha256:" + "a" * 64
    target = graph.targets["application"]
    plan_target = BuildPlanTarget(
        name="application",
        reasons=("changed:images/application/Dockerfile",),
        dependencies=("base",),
        needs=(),
        dockerfile=target.dockerfile.relative_to(root).as_posix(),
        context=target.context.relative_to(root).as_posix(),
        output_ref="registry.example.com/platform-images/application:ci-42-deadbeef",
        input_refs={"base": reference},
        push=True,
        build_contexts=graph.build_contexts("application", {"base": reference}),
    )
    plan = BuildPlan(
        1,
        BuildMode.MERGE_REQUEST,
        "base-sha",
        "deadbeef",
        (plan_target,),
        (),
    )

    rendered = render_bake(plan, config)

    assert f'"base" = "docker-image://{reference}"' in rendered
    assert "target:image-base" not in rendered


def test_bake_renders_an_empty_ci_plan_as_a_valid_noop(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config, _graph = _state(root)
    plan = BuildPlan(1, BuildMode.MERGE_REQUEST, "before", "head", (), ())

    rendered = render_bake(plan, config)

    assert "No images are affected" in rendered
    assert rendered.count("targets = [\n  ]") == 2
    assert "\ntarget " not in rendered


def test_generate_bake_defaults_to_all_and_writes_inside_repository(
    repository_factory: Callable[[dict[str, str]], Path], capsys
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "application": "FROM base\n"})

    assert (
        main(
            ["images", "generate-bake", "--output", "generated/docker-bake.hcl"],
            cwd=root,
        )
        == 0
    )
    assert capsys.readouterr().out == "generated/docker-bake.hcl\n"
    rendered = (root / "generated/docker-bake.hcl").read_text(encoding="utf-8")
    assert 'group "all" {' in rendered
    assert 'target "image-base" {' in rendered
    assert 'target "image-application" {' in rendered


def test_generate_bake_can_render_a_persisted_plan(
    repository_factory: Callable[[dict[str, str]], Path], capsys
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "application": "FROM base\n"})
    config, graph = _state(root)
    plan = local_plan(graph, config, frozenset({"application"}))
    (root / "image-plan.json").write_text(render_plan_json(plan), encoding="utf-8")

    assert main(["images", "generate-bake", "--plan", "image-plan.json"], cwd=root) == 0
    rendered = capsys.readouterr().out
    assert 'group "selected" {' in rendered
    assert '"base" = "target:image-base"' in rendered


def test_generate_bake_treats_an_empty_ci_change_as_a_successful_noop(
    git_repository: Path, capsys
) -> None:
    head = git(git_repository, "rev-parse", "HEAD")
    environment = {
        "PLATFORM_IMAGES_REGISTRY": "registry.example.com",
        "CI_COMMIT_SHA": head,
        "CI_MERGE_REQUEST_DIFF_BASE_SHA": head,
        "CI_MERGE_REQUEST_IID": "42",
        "CI_PIPELINE_ID": "9001",
        "CI_PROJECT_URL": "https://example.com/team/repository",
    }

    assert (
        main(
            ["images", "generate-bake", "--ci"],
            cwd=git_repository,
            environment=environment,
        )
        == 0
    )
    rendered = capsys.readouterr().out
    assert 'group "affected" {' in rendered
    assert "No images are affected" in rendered
    assert "\ntarget " not in rendered


def test_generate_bake_rejects_conflicting_selectors_and_external_output(
    repository_factory: Callable[[dict[str, str]], Path], tmp_path: Path, capsys
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    config, graph = _state(root)
    (root / "image-plan.json").write_text(
        render_plan_json(local_plan(graph, config, frozenset({"base"}))),
        encoding="utf-8",
    )

    assert (
        main(
            ["images", "generate-bake", "--plan", "image-plan.json", "--all"],
            cwd=root,
        )
        == 1
    )
    assert "--plan cannot be combined" in capsys.readouterr().err

    assert (
        main(
            ["images", "generate-bake", "--output", str(tmp_path / "outside.hcl")],
            cwd=root,
        )
        == 1
    )
    assert "Bake output must stay within the repository" in capsys.readouterr().err
