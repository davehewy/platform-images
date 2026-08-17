#!/usr/bin/env python3
"""Reproducible benchmark for discovery, graph planning, and generated CI pipelines."""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from platform_images.benchmark_scenarios import (
    IMPERFECT_DISCOVERY_ROOTS,
    write_imperfect_repository,
)
from platform_images.changes import map_changes
from platform_images.config import RepositoryConfig
from platform_images.models import BuildMode, ChangedPath
from platform_images.planner import affected_reasons, all_ci_plan, local_plan
from platform_images.renderers.github import graph_layer_count, render_github_workflow
from platform_images.renderers.gitlab import render_gitlab
from platform_images.renderers.graph_text import render_graph_text
from platform_images.renderers.plan_json import render_plan_json
from platform_images.validation import validate_repository

CHAIN_DISCOVERY_ROOTS = (
    "containers/shared",
    "services/payments/images",
    "platform/runtime/containers",
)
SCENARIOS = ("imperfect", "chain")


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def measure(operation: Callable[[], object], iterations: int) -> dict[str, float]:
    for _ in range(min(3, iterations)):
        operation()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return {
        "median_ms": round(statistics.median(samples), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
    }


def write_chain_repository(root: Path, image_count: int) -> dict[str, str]:
    roots_toml = ", ".join(json.dumps(value) for value in CHAIN_DISCOVERY_ROOTS)
    (root / "platform-images.toml").write_text(
        f"""\
[registry]
namespace = "platform-images"
registry_environment_variable = "PLATFORM_IMAGES_REGISTRY"
stable_tag = "main"

[tags]
ci_prefix = "ci"
commit_prefix = "sha"

[discovery]
roots = [{roots_toml}]

[dockerfile]
allowed_short_external_images = ["alpine"]

[changes]
global_inputs = []
""",
        encoding="utf-8",
    )
    locations: dict[str, str] = {}
    for index in range(image_count):
        name = f"image-{index:03d}"
        discovery_root = CHAIN_DISCOVERY_ROOTS[index % len(CHAIN_DISCOVERY_ROOTS)]
        directory = root / discovery_root / name
        directory.mkdir(parents=True, exist_ok=True)
        lines = ["FROM alpine:3.22"] if index == 0 else [f"FROM image-{index - 1:03d}"]
        extra_dependencies = {index // 2, max(0, index - 7)} - {index, index - 1}
        for dependency in sorted(extra_dependencies):
            lines.append(
                f"COPY --from=image-{dependency:03d} /etc/os-release /tmp/release-{dependency}"
            )
        (directory / "Dockerfile").write_text("\n".join(lines) + "\n", encoding="utf-8")
        locations[name] = f"{discovery_root}/{name}"
    return locations


def run_benchmark(image_count: int, iterations: int, scenario: str = "imperfect") -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="platform-images-benchmark-") as temporary:
        root = Path(temporary)
        if scenario == "imperfect":
            corpus = write_imperfect_repository(root, image_count)
            locations = corpus.locations
            expected_warnings = len(corpus.probable_local_warnings)
            discovery_root_count = len(IMPERFECT_DISCOVERY_ROOTS)
            ignored_directory_count = len(corpus.ignored_directories)
        elif scenario == "chain":
            locations = write_chain_repository(root, image_count)
            expected_warnings = 0
            discovery_root_count = len(CHAIN_DISCOVERY_ROOTS)
            ignored_directory_count = 0
        else:
            raise ValueError(f"unknown benchmark scenario: {scenario}")
        config = RepositoryConfig.load(root)
        report = validate_repository(config)
        if not report.valid:
            messages = "; ".join(issue.message for issue in report.errors)
            raise RuntimeError(f"synthetic repository did not validate: {messages}")
        if len(report.warnings) != expected_warnings:
            raise RuntimeError(
                f"expected {expected_warnings} deterministic warnings, "
                f"received {len(report.warnings)}"
            )
        graph = report.graph
        leaf = next(reversed(locations))
        root_target = next(iter(locations))
        root_change = ChangedPath(
            "M",
            None,
            f"{locations[root_target]}/{graph.targets[root_target].dockerfile.name}",
        )
        expected_leaf_targets = len(graph.upstream_closure(frozenset({leaf})))

        def validate_and_parse() -> object:
            candidate = validate_repository(config)
            if (
                not candidate.valid
                or len(candidate.graph.targets) != image_count
                or len(candidate.warnings) != expected_warnings
            ):
                raise RuntimeError("validation benchmark produced an invalid graph")
            return candidate

        def plan_leaf() -> object:
            plan = local_plan(graph, config, frozenset({leaf}))
            if len(plan.targets) != expected_leaf_targets:
                raise RuntimeError("leaf plan did not include its complete upstream graph")
            return plan

        def change_and_affected() -> object:
            changes = map_changes((root_change,), graph.targets, config)
            reasons = affected_reasons(graph, changes)
            if len(reasons) != image_count:
                raise RuntimeError("root change did not affect every downstream target")
            return reasons

        def ci_plan_and_render() -> object:
            plan = all_ci_plan(
                graph,
                config,
                head_sha="a" * 40,
                mode=BuildMode.MERGE_REQUEST,
                registry="registry.example.com",
                pipeline_id="12345",
            )
            plan_json = render_plan_json(plan)
            # Benchmark rendering independently of the GitLab tier selected by a real project.
            gitlab = render_gitlab(
                plan,
                config,
                "registry.example.com",
                max_jobs=len(plan.targets) + 2,
            )
            github = render_github_workflow(graph, config)
            if (
                len(plan.targets) != image_count
                or plan_json.count('"output_ref"') != image_count
                or gitlab.count("extends: .image-build") < image_count
                or github.count("image_layer_") < graph_layer_count(graph)
            ):
                raise RuntimeError("generated CI output omitted image targets or dependency layers")
            return plan_json, gitlab, github

        def render_terminal_graph() -> object:
            rendered = render_graph_text(graph, ascii_only=True)
            line_count = len(rendered.splitlines())
            maximum_lines = 1 + len(graph.roots()) + sum(map(len, graph.dependencies.values())) + 3
            if line_count > maximum_lines:
                raise RuntimeError("terminal graph expanded shared DAG subtrees more than once")
            return rendered

        terminal_graph_lines = len(str(render_terminal_graph()).splitlines())

        return {
            "scenario": scenario,
            "images": image_count,
            "edges": sum(len(dependencies) for dependencies in graph.dependencies.values()),
            "discovery_roots": discovery_root_count,
            "graph_layers": graph_layer_count(graph),
            "warnings": len(report.warnings),
            "ignored_directories": ignored_directory_count,
            "leaf_plan_targets": expected_leaf_targets,
            "terminal_graph_lines": terminal_graph_lines,
            "iterations": iterations,
            "operations": {
                "validate_and_parse": measure(validate_and_parse, iterations),
                "topological_all": measure(graph.topological_order, iterations),
                "local_plan_leaf": measure(plan_leaf, iterations),
                "change_and_affected": measure(change_and_affected, iterations),
                "render_terminal_graph": measure(render_terminal_graph, iterations),
                "ci_plan_and_render": measure(ci_plan_and_render, iterations),
            },
        }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--scenario", choices=SCENARIOS, default="imperfect")
    result.add_argument("--images", type=positive_integer, default=360)
    result.add_argument("--iterations", type=positive_integer, default=25)
    result.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    result.add_argument(
        "--max-leaf-plan-ms",
        type=float,
        default=None,
        help="fail if the median deep-leaf planning time exceeds this threshold",
    )
    result.add_argument(
        "--max-validate-ms",
        type=float,
        default=None,
        help="fail if median discovery, parsing, and validation exceeds this threshold",
    )
    result.add_argument(
        "--max-ci-render-ms",
        type=float,
        default=None,
        help="fail if median all-image planning and GitHub/GitLab rendering exceeds this threshold",
    )
    result.add_argument(
        "--max-graph-render-ms",
        type=float,
        default=None,
        help="fail if median bounded terminal graph rendering exceeds this threshold",
    )
    return result


def main() -> int:
    argument_parser = parser()
    arguments = argument_parser.parse_args()
    if arguments.scenario == "imperfect" and arguments.images < 30:
        argument_parser.error("--scenario imperfect requires --images of at least 30")
    result = run_benchmark(arguments.images, arguments.iterations, arguments.scenario)
    if arguments.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"{result['scenario'].title()} DAG: {result['images']} images, "
            f"{result['edges']} edges, {result['graph_layers']} layers, "
            f"{result['discovery_roots']} discovery roots, {result['warnings']} warnings"
        )
        for name, timing in result["operations"].items():
            print(
                f"{name:22} median {timing['median_ms']:8.3f} ms  "
                f"min {timing['min_ms']:8.3f} ms  max {timing['max_ms']:8.3f} ms"
            )
    thresholds = {
        "validate_and_parse": arguments.max_validate_ms,
        "local_plan_leaf": arguments.max_leaf_plan_ms,
        "render_terminal_graph": arguments.max_graph_render_ms,
        "ci_plan_and_render": arguments.max_ci_render_ms,
    }
    for operation, threshold in thresholds.items():
        median = result["operations"][operation]["median_ms"]
        if threshold is not None and median > threshold:
            print(f"{operation} median {median:.3f} ms exceeds {threshold:.3f} ms")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
