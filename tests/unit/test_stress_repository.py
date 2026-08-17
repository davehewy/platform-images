from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from platform_images.benchmark_scenarios import ImperfectRepository, write_imperfect_repository
from platform_images.changes import map_changes
from platform_images.cli import main
from platform_images.config import RepositoryConfig
from platform_images.models import BuildMode, ChangedPath
from platform_images.planner import (
    affected_reasons,
    all_ci_plan,
    change_plan,
    validate_plan_against_graph,
)
from platform_images.registry import StaticRegistryClient
from platform_images.renderers.github import (
    graph_layer_count,
    render_github_outputs,
    render_github_workflow,
)
from platform_images.renderers.gitlab import job_name, render_gitlab
from platform_images.renderers.graph_text import render_graph_text
from platform_images.renderers.plan_json import load_plan_json, render_plan_json
from platform_images.validation import ValidationReport, validate_repository

IMAGE_COUNT = 360


def test_init_cascades_one_repository_policy_across_320_images(tmp_path: Path, capsys) -> None:
    root = tmp_path / "large-adoption-repository"
    targets = root / "owned" / "container-images"

    def logical_name(index: int) -> str:
        return f"service{index:03d}" if index % 40 == 0 else f"service-{index:03d}"

    def remote_name(index: int) -> str:
        return f"service-{index:03d}"

    for index in range(320):
        directory = targets / logical_name(index)
        directory.mkdir(parents=True)
        source = (
            "docker.io/library/alpine:3.22"
            if index == 0
            else f"nexus.example.com:8088/risk-repo/{remote_name(index - 1)}:latest"
        )
        (directory / "Dockerfile").write_text(f"FROM {source}\n", encoding="utf-8")

    assert main(["init"], cwd=root, environment={}) == 0

    config = RepositoryConfig.load(root)
    report = validate_repository(config)
    assert report.valid
    assert not report.warnings
    assert config.registry.namespace == "risk-repo"
    assert len(config.images) == 8
    assert all(
        config.image_repository(logical_name(index)) == f"risk-repo/{remote_name(index)}"
        for index in range(0, 320, 40)
    )
    assert sum(map(len, report.graph.dependencies.values())) == 319
    assert report.graph.topological_order() == tuple(logical_name(index) for index in range(320))
    output = capsys.readouterr().out
    assert "Discovered 320 image targets" in output
    assert "init applied 8 inferred repository mappings" in output


@pytest.fixture(scope="module")
def imperfect_repository(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, ImperfectRepository, RepositoryConfig, ValidationReport]:
    root = tmp_path_factory.mktemp("imperfect-container-monorepo")
    corpus = write_imperfect_repository(root, IMAGE_COUNT)
    config = RepositoryConfig.load(root)
    return root, corpus, config, validate_repository(config)


def test_360_image_imperfect_repository_resolves_every_intended_edge_and_warning(
    imperfect_repository: tuple[Path, ImperfectRepository, RepositoryConfig, ValidationReport],
) -> None:
    root, corpus, config, report = imperfect_repository

    assert report.valid
    assert len(report.graph.targets) == IMAGE_COUNT
    assert report.graph.dependencies == corpus.dependencies
    assert sum(map(len, report.graph.dependencies.values())) == 737
    assert corpus.dockerfile_count == 240
    assert corpus.containerfile_count == 120
    assert len(corpus.ignored_directories) == 28
    assert all(not (root / path / "Dockerfile").exists() for path in corpus.ignored_directories)
    assert set(config.discovery.roots) >= {"utils", "utils/container-images"}

    warnings = tuple(issue for issue in report.issues if issue.code == "probable-local-reference")
    assert len(warnings) == len(corpus.probable_local_warnings) == 8
    for consumer, (raw, likely_target) in corpus.probable_local_warnings.items():
        issue = next(issue for issue in warnings if raw in issue.message)
        assert issue.path == report.graph.targets[consumer].dockerfile.relative_to(root).as_posix()
        assert issue.hint is not None
        assert f'[images."{likely_target}"]' in issue.hint
    warning_messages = "\n".join(issue.message for issue in warnings)
    for consumer, reference in corpus.explicit_external_references.items():
        assert reference not in warning_messages
        assert any(
            item.resolved == f"{reference}:stable"
            for item in report.graph.external_dependencies[consumer]
        )

    order = report.graph.topological_order()
    positions = {target: index for index, target in enumerate(order)}
    assert all(
        positions[dependency] < positions[target]
        for target, dependencies in corpus.dependencies.items()
        for dependency in dependencies
    )
    assert graph_layer_count(report.graph) == 25
    assert report.graph.downstream_closure({corpus.root_target}) == frozenset(corpus.names)
    assert len(report.graph.upstream_closure({corpus.leaf_target})) == 202

    references = tuple(
        reference
        for result in report.graph.parse_results.values()
        for reference in result.references
    )
    assert {reference.instruction for reference in references} >= {
        "FROM",
        "COPY --from",
        "ADD --from",
        "RUN --mount=from",
    }
    assert any(
        (reference.source or "").startswith("nexus.example.com/") for reference in references
    )
    assert any("/published/" in (reference.source or "") for reference in references)
    assert any((reference.source or "").startswith("ghcr.io/") for reference in references)
    assert any("${SOURCE_REGISTRY}" in reference.raw for reference in references)
    assert all("inside-heredoc" not in reference.raw for reference in references)


def test_360_image_change_impact_and_dynamic_ci_outputs_are_complete_and_deterministic(
    imperfect_repository: tuple[Path, ImperfectRepository, RepositoryConfig, ValidationReport],
) -> None:
    _root, corpus, config, report = imperfect_repository
    graph = report.graph
    root_target = graph.targets[corpus.root_target]
    leaf_target = graph.targets[corpus.leaf_target]
    root_change = ChangedPath(
        "M",
        None,
        root_target.dockerfile.relative_to(config.root).as_posix(),
    )
    leaf_change = ChangedPath(
        "M",
        None,
        (leaf_target.directory / "application.conf").relative_to(config.root).as_posix(),
    )

    root_changes = map_changes((root_change,), graph.targets, config)
    assert root_changes.changed_targets == {corpus.root_target}
    assert set(affected_reasons(graph, root_changes)) == set(corpus.names)

    leaf_changes = map_changes((leaf_change,), graph.targets, config)
    assert leaf_changes.changed_targets == {corpus.leaf_target}
    assert affected_reasons(graph, leaf_changes) == {
        corpus.leaf_target: (f"source-changed:{leaf_change.new_path}",)
    }

    ignored_change = ChangedPath("M", None, f"{corpus.ignored_directories[0]}/README.md")
    ignored = map_changes((ignored_change,), graph.targets, config)
    assert not ignored.changed_targets
    assert not ignored.global_change

    global_change = ChangedPath("M", None, "build/policy/rebuild-rules.toml")
    global_changes = map_changes((global_change,), graph.targets, config)
    assert global_changes.global_change
    assert global_changes.changed_targets == frozenset(corpus.names)

    plan = change_plan(
        graph,
        config,
        root_changes,
        base_sha="b" * 40,
        head_sha="a" * 40,
        mode=BuildMode.MERGE_REQUEST,
        registry="registry.example.com",
        pipeline_id="42",
        registry_client=StaticRegistryClient({}),
    )
    assert len(plan.targets) == IMAGE_COUNT
    assert tuple(target.name for target in plan.targets) == graph.topological_order()
    assert len({target.output_ref for target in plan.targets}) == IMAGE_COUNT

    rendered_plan = render_plan_json(plan)
    loaded_plan = load_plan_json(rendered_plan)
    assert loaded_plan == plan
    validate_plan_against_graph(loaded_plan, graph, config)

    gitlab = render_gitlab(plan, config, "registry.example.com")
    gitlab_document = yaml.safe_load(gitlab)
    for target in plan.targets:
        job = gitlab_document[job_name(target.name)]
        assert job.get("needs", []) == [job_name(dependency) for dependency in target.needs]
        assert target.output_ref in job["script"][0]
    assert render_gitlab(plan, config, "registry.example.com") == gitlab

    github = render_github_workflow(graph, config)
    github_document = yaml.safe_load(github)
    github_jobs = github_document["jobs"]
    layer_jobs = {name: job for name, job in github_jobs.items() if name.startswith("image_layer_")}
    assert len(layer_jobs) == graph_layer_count(graph) == 25
    assert render_github_workflow(graph, config) == github

    matrix_output = render_github_outputs(plan, len(layer_jobs))
    matrix_lines = dict(line.split("=", 1) for line in matrix_output.splitlines())
    matrix_targets = {
        entry["target"]
        for index in range(len(layer_jobs))
        for entry in json.loads(matrix_lines[f"layer_{index}"])["include"]
        if entry["target"]
    }
    assert matrix_targets == set(corpus.names)


def test_360_image_all_ci_plan_covers_every_target_without_registry_reads(
    imperfect_repository: tuple[Path, ImperfectRepository, RepositoryConfig, ValidationReport],
) -> None:
    _root, corpus, config, report = imperfect_repository
    plan = all_ci_plan(
        report.graph,
        config,
        head_sha="c" * 40,
        mode=BuildMode.DEFAULT_BRANCH,
        registry="nexus.example.com",
        pipeline_id="9001",
    )

    assert len(plan.targets) == IMAGE_COUNT
    assert {target.name for target in plan.targets} == set(corpus.names)
    assert all(set(target.input_refs) == set(target.dependencies) for target in plan.targets)
    assert all(set(target.needs) == set(target.dependencies) for target in plan.targets)


def test_360_image_cli_validation_and_terminal_graph_stay_bounded(
    imperfect_repository: tuple[Path, ImperfectRepository, RepositoryConfig, ValidationReport],
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, corpus, _config, report = imperfect_repository

    assert main(["images", "validate", "--format", "json"], cwd=root, environment={}) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["valid"] is True
    assert len(validation["warnings"]) == 8

    assert main(["images", "graph", "--ascii"], cwd=root, environment={}) == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith("Container image dependency graph\n\\-- foundation-000\n")
    assert "[already shown] incoming edge retained; repeated subtree omitted" in rendered
    assert len(rendered.splitlines()) == 742
    assert rendered == render_graph_text(report.graph, ascii_only=True) + "\n"
    assert all(name in rendered for name in corpus.names)


def test_360_image_cycle_is_rejected_with_the_same_complete_path(tmp_path: Path) -> None:
    corpus = write_imperfect_repository(tmp_path, IMAGE_COUNT)
    config = RepositoryConfig.load(tmp_path)
    root_file = config.root / corpus.locations[corpus.root_target] / "Dockerfile"
    root_file.write_text(
        root_file.read_text(encoding="utf-8") + f"COPY --from={corpus.leaf_target} /cycle /cycle\n",
        encoding="utf-8",
    )

    first = validate_repository(config)
    second = validate_repository(config)
    cycles = [issue for issue in first.errors if issue.code == "dependency-cycle"]

    assert len(cycles) == 1
    assert first.graph.find_cycle() == second.graph.find_cycle()
    cycle = first.graph.find_cycle()
    assert cycle is not None
    assert cycle[0] == cycle[-1]
    assert corpus.root_target in cycle
    assert corpus.leaf_target in cycle
    assert " -> ".join(cycle) in cycles[0].message


def test_360_image_ambiguous_and_missing_relations_fail_without_guessing(tmp_path: Path) -> None:
    corpus = write_imperfect_repository(tmp_path, IMAGE_COUNT)
    config_path = tmp_path / "platform-images.toml"
    shared_alias = "nexus.example.com/shared/ambiguous-base"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f'''\
[images."{corpus.names[1]}"]
aliases = ["{shared_alias}"]

[images."{corpus.names[2]}"]
aliases = ["{shared_alias}"]
''',
        encoding="utf-8",
    )
    consumer_file = next(
        candidate
        for candidate in (tmp_path / corpus.locations[corpus.names[30]]).iterdir()
        if candidate.name in {"Dockerfile", "Containerfile"}
    )
    consumer_file.write_text(
        "".join(
            (
                consumer_file.read_text(encoding="utf-8"),
                f"COPY --from={shared_alias}:latest /ambiguous /ambiguous\n",
                "COPY --from=nexus.example.com/platform/base-images/not-present:latest "
                "/missing /missing\n",
                "RUN echo unterminated \\\n",
            )
        ),
        encoding="utf-8",
    )

    first = validate_repository(RepositoryConfig.load(tmp_path))
    second = validate_repository(RepositoryConfig.load(tmp_path))
    codes = [issue.code for issue in first.errors]

    assert codes.count("duplicate-image-identity") == 2
    assert codes.count("ambiguous-local-reference") == 1
    assert codes.count("missing-internal-target") == 1
    assert codes.count("invalid-dockerfile-syntax") == 1
    assert first.errors == second.errors
