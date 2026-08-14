#!/usr/bin/env python3
"""Reproducible synthetic benchmark for discovery, graph construction, and planning."""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from platform_images.changes import map_changes
from platform_images.config import RepositoryConfig
from platform_images.models import ChangedPath
from platform_images.planner import affected_reasons, local_plan
from platform_images.validation import validate_repository

DISCOVERY_ROOTS = (
    "containers/shared",
    "services/payments/images",
    "platform/runtime/containers",
)


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


def write_repository(root: Path, image_count: int) -> dict[str, str]:
    roots_toml = ", ".join(json.dumps(value) for value in DISCOVERY_ROOTS)
    (root / "platform-images.toml").write_text(
        f'''\
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
''',
        encoding="utf-8",
    )
    locations: dict[str, str] = {}
    for index in range(image_count):
        name = f"image-{index:03d}"
        discovery_root = DISCOVERY_ROOTS[index % len(DISCOVERY_ROOTS)]
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


def run_benchmark(image_count: int, iterations: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="platform-images-benchmark-") as temporary:
        root = Path(temporary)
        locations = write_repository(root, image_count)
        config = RepositoryConfig.load(root)
        report = validate_repository(config)
        if not report.valid:
            messages = "; ".join(issue.message for issue in report.errors)
            raise RuntimeError(f"synthetic repository did not validate: {messages}")
        graph = report.graph
        leaf = f"image-{image_count - 1:03d}"
        root_change = ChangedPath(
            "M",
            None,
            f"{locations['image-000']}/Dockerfile",
        )

        def validate_and_parse() -> object:
            candidate = validate_repository(config)
            if not candidate.valid or len(candidate.graph.targets) != image_count:
                raise RuntimeError("validation benchmark produced an invalid graph")
            return candidate

        def plan_leaf() -> object:
            plan = local_plan(graph, config, frozenset({leaf}))
            if len(plan.targets) != image_count:
                raise RuntimeError("leaf plan did not include the complete upstream graph")
            return plan

        def change_and_affected() -> object:
            changes = map_changes((root_change,), graph.targets, config)
            reasons = affected_reasons(graph, changes)
            if len(reasons) != image_count:
                raise RuntimeError("root change did not affect every downstream target")
            return reasons

        return {
            "images": image_count,
            "edges": sum(len(dependencies) for dependencies in graph.dependencies.values()),
            "discovery_roots": len(DISCOVERY_ROOTS),
            "iterations": iterations,
            "operations": {
                "validate_and_parse": measure(validate_and_parse, iterations),
                "topological_all": measure(graph.topological_order, iterations),
                "local_plan_leaf": measure(plan_leaf, iterations),
                "change_and_affected": measure(change_and_affected, iterations),
            },
        }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--images", type=positive_integer, default=100)
    result.add_argument("--iterations", type=positive_integer, default=25)
    result.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    result.add_argument(
        "--max-leaf-plan-ms",
        type=float,
        default=None,
        help="fail if the median deep-leaf planning time exceeds this threshold",
    )
    return result


def main() -> int:
    arguments = parser().parse_args()
    result = run_benchmark(arguments.images, arguments.iterations)
    if arguments.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"Synthetic DAG: {result['images']} images, {result['edges']} edges, "
            f"{result['discovery_roots']} discovery roots"
        )
        for name, timing in result["operations"].items():
            print(
                f"{name:22} median {timing['median_ms']:8.3f} ms  "
                f"min {timing['min_ms']:8.3f} ms  max {timing['max_ms']:8.3f} ms"
            )
    threshold = arguments.max_leaf_plan_ms
    leaf_median = result["operations"]["local_plan_leaf"]["median_ms"]
    if threshold is not None and leaf_median > threshold:
        print(
            f"local_plan_leaf median {leaf_median:.3f} ms exceeds {threshold:.3f} ms",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
