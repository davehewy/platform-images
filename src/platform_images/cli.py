from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from platform_images.build import (
    execute_ci_build,
    execute_local_plan,
    parse_input_references,
    result_json,
)
from platform_images.changes import (
    ZERO_SHA,
    GitClient,
    detect_changes,
    validate_removed_references,
)
from platform_images.config import RepositoryConfig
from platform_images.discovery import discover_targets
from platform_images.errors import PlatformImagesError
from platform_images.models import BuildEngine, BuildMode, BuildPlan, ChangeSet
from platform_images.planner import (
    affected_reasons,
    all_ci_plan,
    change_plan,
    local_plan,
    validate_plan_against_graph,
)
from platform_images.references import ReferencePolicy
from platform_images.registry import (
    ContainerRegistryClient,
    ECRRegistryClient,
    login_to_ecr,
    registry_from_environment_json,
)
from platform_images.renderers.github import render_github_outputs, render_github_workflow
from platform_images.renderers.gitlab import render_gitlab
from platform_images.renderers.graph_json import graph_data, render_graph_json
from platform_images.renderers.graph_text import render_graph_text
from platform_images.renderers.plan_json import load_plan_json, render_plan_json
from platform_images.renderers.plan_text import render_plan_text
from platform_images.validation import ValidationIssue, ValidationReport, validate_repository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="platform")
    root_subparsers = parser.add_subparsers(dest="group", required=True)
    images = root_subparsers.add_parser("images", help="manage repository image targets")
    images.add_argument(
        "--root",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    commands = images.add_subparsers(dest="command", required=True)

    commands.add_parser("list")

    show = commands.add_parser("show")
    show.add_argument("name")
    show.add_argument("--format", choices=("text", "json"), default="text")

    validate = commands.add_parser("validate")
    validate.add_argument("--format", choices=("text", "json"), default="text")

    graph = commands.add_parser("graph")
    graph.add_argument("--format", choices=("text", "json"), default="text")

    build = commands.add_parser("build")
    build.add_argument("name")
    build.add_argument("--dry-run", action="store_true")
    build.add_argument("--no-deps", action="store_true")
    build.add_argument("--engine", choices=tuple(engine.value for engine in BuildEngine))

    for name in ("changed", "affected"):
        command = commands.add_parser(name)
        command.add_argument("--base", required=True)
        command.add_argument("--head", required=True)
        command.add_argument("--format", choices=("text", "json"), default="text")

    plan = commands.add_parser("plan")
    plan.add_argument("--all", action="store_true")
    plan.add_argument("--image", action="append", default=[])
    plan.add_argument("--base")
    plan.add_argument("--head")
    plan.add_argument("--ci", action="store_true")
    plan.add_argument("--format", choices=("text", "json", "gitlab"), default="text")

    render_plan = commands.add_parser("render-plan")
    render_plan.add_argument("plan_file", type=Path)
    render_plan.add_argument("--format", choices=("text", "json", "gitlab"), default="gitlab")

    ci_build = commands.add_parser("ci-build")
    ci_build.add_argument("name")
    ci_build.add_argument("--output-ref", required=True)
    ci_build.add_argument("--input-ref", action="append", default=[])
    ci_build.add_argument("--engine", choices=tuple(engine.value for engine in BuildEngine))

    registry_login = commands.add_parser("registry-login")
    registry_login.add_argument("--engine", choices=tuple(engine.value for engine in BuildEngine))

    promote_parser = commands.add_parser("promote")
    promote_parser.add_argument("--source", required=True)
    promote_parser.add_argument("--destination", required=True)
    promote_parser.add_argument("--engine", choices=tuple(engine.value for engine in BuildEngine))

    build_plan_target = commands.add_parser("build-plan-target")
    build_plan_target.add_argument("plan_file", type=Path)
    build_plan_target.add_argument("name")
    build_plan_target.add_argument(
        "--engine", choices=tuple(engine.value for engine in BuildEngine)
    )

    promote_plan = commands.add_parser("promote-plan")
    promote_plan.add_argument("plan_file", type=Path)
    promote_plan.add_argument("--engine", choices=tuple(engine.value for engine in BuildEngine))

    github_matrix = commands.add_parser("github-matrix")
    github_matrix.add_argument("plan_file", type=Path)
    github_matrix.add_argument("--max-layers", type=int, required=True)

    workflow = commands.add_parser("generate-workflow")
    workflow.add_argument("provider", choices=("github",))
    workflow.add_argument("--default-branch", default="main")
    workflow.add_argument("--runner", default="ubuntu-latest")
    workflow.add_argument(
        "--engine",
        choices=tuple(engine.value for engine in BuildEngine),
        default=BuildEngine.DOCKER.value,
    )
    workflow.add_argument("--aws-auth", choices=("oidc", "ambient"), default="oidc")
    workflow.add_argument("--aws-role-variable", default="AWS_ROLE_TO_ASSUME")
    workflow.add_argument("--aws-region-variable", default="AWS_REGION")
    workflow.add_argument("--output", type=Path)
    return parser


def _root(arguments: argparse.Namespace, cwd: Path | None) -> Path:
    configured = arguments.root or os.environ.get("PLATFORM_IMAGES_ROOT")
    return Path(configured).resolve() if configured else (cwd or Path.cwd()).resolve()


def _issue_data(issue: ValidationIssue) -> dict[str, object]:
    return {
        "code": issue.code,
        "severity": issue.severity,
        "path": issue.path,
        "line": issue.line,
        "message": issue.message,
        "hint": issue.hint,
    }


def _render_validation(report: ValidationReport, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(
            {
                "schema_version": 1,
                "valid": report.valid,
                "errors": [_issue_data(issue) for issue in report.errors],
                "warnings": [_issue_data(issue) for issue in report.warnings],
            },
            indent=2,
        )
    if report.valid:
        summary = f"Validation passed ({len(report.graph.targets)} image targets)."
        if report.warnings:
            summary += f"\n\nWarnings: {len(report.warnings)}"
        return summary
    lines = [f"Validation failed with {len(report.errors)} errors:"]
    for issue in report.errors:
        location = issue.path or "repository"
        if issue.line is not None:
            location += f":{issue.line}"
        lines.extend(["", location, f"  {issue.message}"])
        if issue.hint:
            lines.append(f"  {issue.hint}")
    return "\n".join(lines)


def _require_valid(report: ValidationReport) -> None:
    if not report.valid:
        raise PlatformImagesError(_render_validation(report, "text"))


def _changes_data(changes: ChangeSet) -> dict[str, object]:
    return {
        "schema_version": 1,
        "changed_files": [
            {
                "status": item.status,
                "old_path": item.old_path,
                "new_path": item.new_path,
            }
            for item in changes.changed_files
        ],
        "changed_targets": sorted(changes.changed_targets),
        "removed_targets": sorted(changes.removed_targets),
        "global_change": changes.global_change,
        "reasons": {name: list(values) for name, values in changes.reasons.items()},
    }


def _ci_mode(environment: Mapping[str, str]) -> BuildMode:
    if (
        environment.get("CI_MERGE_REQUEST_IID")
        or environment.get("CI_PIPELINE_SOURCE") == "merge_request_event"
    ):
        return BuildMode.MERGE_REQUEST
    if environment.get("CI_COMMIT_BRANCH") and environment.get(
        "CI_COMMIT_BRANCH"
    ) == environment.get("CI_DEFAULT_BRANCH"):
        return BuildMode.DEFAULT_BRANCH
    return BuildMode.MERGE_REQUEST


def _ci_base_head(
    environment: Mapping[str, str], git: GitClient
) -> tuple[str | None, str, BuildMode]:
    head = environment.get("CI_COMMIT_SHA")
    if not head:
        raise PlatformImagesError("CI_COMMIT_SHA is required for --ci planning")
    mode = _ci_mode(environment)
    if mode is BuildMode.MERGE_REQUEST and environment.get("CI_MERGE_REQUEST_DIFF_BASE_SHA"):
        return environment["CI_MERGE_REQUEST_DIFF_BASE_SHA"], head, mode
    before = environment.get("CI_COMMIT_BEFORE_SHA")
    if before and before != ZERO_SHA:
        return before, head, mode
    # GitLab uses the all-zero before SHA when no previous branch commit exists (for example the
    # first default-branch pipeline). Comparing HEAD with its merge-base against origin/main would
    # compare HEAD to itself and silently build nothing. A default-branch bootstrap must build the
    # complete graph.
    if mode is BuildMode.DEFAULT_BRANCH:
        return None, head, mode
    default_branch = environment.get("CI_DEFAULT_BRANCH")
    if default_branch:
        try:
            return git.merge_base(head, f"origin/{default_branch}"), head, mode
        except PlatformImagesError as exc:
            raise PlatformImagesError(
                f"the CI comparison base is unavailable: {exc}\n"
                "Fetch full Git history (GIT_DEPTH=0) or explicitly request --ci --all."
            ) from exc
    raise PlatformImagesError(
        "the CI comparison base is unavailable; set CI_MERGE_REQUEST_DIFF_BASE_SHA or "
        "CI_COMMIT_BEFORE_SHA, or explicitly request --ci --all"
    )


def _registry_client(config: RepositoryConfig, registry: str, env: Mapping[str, str]):
    static = registry_from_environment_json(env.get("PLATFORM_IMAGES_STABLE_REFS"))
    return static or ECRRegistryClient(config, registry)


def _engine(arguments: argparse.Namespace, config: RepositoryConfig) -> BuildEngine:
    selected = getattr(arguments, "engine", None)
    return BuildEngine(selected) if selected else config.build.engine


def _load_plan(path: Path, graph, config: RepositoryConfig) -> BuildPlan:
    try:
        plan = load_plan_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlatformImagesError(f"unable to read persisted plan {path}: {exc}") from exc
    validate_plan_against_graph(plan, graph, config)
    return plan


def _create_plan(
    arguments: argparse.Namespace,
    config: RepositoryConfig,
    report: ValidationReport,
    environment: Mapping[str, str],
) -> BuildPlan:
    graph = report.graph
    selectors = sum(
        (
            bool(arguments.image),
            bool(arguments.all),
            bool(arguments.base or arguments.head),
            bool(arguments.ci),
        )
    )
    if selectors > 1 and not (arguments.all and arguments.ci and selectors == 2):
        raise PlatformImagesError(
            "choose exactly one of --image, --all, --base/--head, or --ci (--ci --all is allowed)"
        )
    if arguments.all and not arguments.ci:
        return local_plan(graph, config, frozenset(graph.targets))
    if arguments.image:
        unknown = sorted(set(arguments.image) - set(graph.targets))
        if unknown:
            raise PlatformImagesError("unknown image target(s): " + ", ".join(unknown))
        return local_plan(graph, config, frozenset(arguments.image))
    if arguments.ci:
        registry = environment.get(config.registry.registry_environment_variable)
        if not registry:
            raise PlatformImagesError(
                f"{config.registry.registry_environment_variable} is required for --ci planning"
            )
        mode = _ci_mode(environment)
        head = environment.get("CI_COMMIT_SHA")
        if not head:
            raise PlatformImagesError("CI_COMMIT_SHA is required for --ci planning")
        if arguments.all:
            return all_ci_plan(
                graph,
                config,
                head_sha=head,
                mode=mode,
                registry=registry,
                pipeline_id=environment.get("CI_PIPELINE_ID"),
            )
        base, head, mode = _ci_base_head(environment, GitClient(config.root))
        if base is None:
            return all_ci_plan(
                graph,
                config,
                head_sha=head,
                mode=mode,
                registry=registry,
                pipeline_id=environment.get("CI_PIPELINE_ID"),
            )
    else:
        if bool(arguments.base) != bool(arguments.head):
            raise PlatformImagesError("--base and --head must be supplied together")
        if not arguments.base:
            raise PlatformImagesError("select --all, --image, --base/--head, or --ci")
        base, head = arguments.base, arguments.head
        mode = _ci_mode(environment)
        registry = environment.get(config.registry.registry_environment_variable)
        if not registry:
            raise PlatformImagesError(
                f"{config.registry.registry_environment_variable} is required for "
                "change-based planning"
            )
    changes = detect_changes(config, graph.targets, base, head)
    return change_plan(
        graph,
        config,
        changes,
        base_sha=base,
        head_sha=head,
        mode=mode,
        registry=registry,
        pipeline_id=environment.get("CI_PIPELINE_ID"),
        registry_client=_registry_client(config, registry, environment),
    )


def _run(arguments: argparse.Namespace, cwd: Path | None, environment: Mapping[str, str]) -> int:
    root = _root(arguments, cwd)
    config = RepositoryConfig.load(root)
    targets = discover_targets(root)

    if arguments.command == "list":
        print("\n".join(sorted(targets)))
        return 0

    report = validate_repository(config)
    if arguments.command == "validate":
        print(_render_validation(report, arguments.format))
        return 0 if report.valid else 1
    _require_valid(report)
    graph = report.graph

    if arguments.command == "show":
        if arguments.name not in graph.targets:
            raise PlatformImagesError(f"unknown image target: {arguments.name}")
        data = graph_data(graph, root)["targets"][arguments.name]  # type: ignore[index]
        if arguments.format == "json":
            print(json.dumps({"schema_version": 1, "name": arguments.name, **data}, indent=2))
        else:
            print(arguments.name)
            print(f"  build_file: {data['dockerfile']}")
            print(f"  dependencies: {', '.join(data['dependencies']) or 'none'}")
            print(f"  dependents: {', '.join(data['dependents']) or 'none'}")
        return 0
    if arguments.command == "graph":
        print(
            render_graph_json(graph, root)
            if arguments.format == "json"
            else render_graph_text(graph, ascii_only=sys.platform == "win32")
        )
        return 0
    if arguments.command == "build":
        if arguments.name not in graph.targets:
            raise PlatformImagesError(f"unknown image target: {arguments.name}")
        plan = local_plan(
            graph,
            config,
            frozenset({arguments.name}),
            include_dependencies=not arguments.no_deps,
        )
        commands = execute_local_plan(
            plan,
            root=root,
            dry_run=arguments.dry_run,
            engine=_engine(arguments, config),
        )
        if arguments.dry_run:
            print("\n".join(commands))
        return 0
    if arguments.command in {"changed", "affected"}:
        changes = detect_changes(config, graph.targets, arguments.base, arguments.head)
        validate_removed_references(graph, changes)
        if arguments.command == "changed":
            if arguments.format == "json":
                print(json.dumps(_changes_data(changes), indent=2))
            else:
                print("\n".join(sorted(changes.changed_targets)))
                for removed in sorted(changes.removed_targets):
                    print(f"removed: {removed}")
        else:
            reasons = affected_reasons(graph, changes)
            ordered = graph.topological_order(frozenset(reasons))
            if arguments.format == "json":
                print(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "targets": list(ordered),
                            "reasons": {name: list(reasons[name]) for name in ordered},
                            "removed_targets": sorted(changes.removed_targets),
                        },
                        indent=2,
                    )
                )
            else:
                print("\n".join(ordered))
        return 0
    if arguments.command == "plan":
        plan = _create_plan(arguments, config, report, environment)
        if arguments.format == "json":
            print(render_plan_json(plan))
        elif arguments.format == "gitlab":
            if plan.mode is BuildMode.LOCAL:
                raise PlatformImagesError("GitLab rendering requires a CI or change-based plan")
            registry = environment.get(config.registry.registry_environment_variable)
            if not registry:
                raise PlatformImagesError("registry is required for GitLab rendering")
            print(render_gitlab(plan, config, registry), end="")
        else:
            print(render_plan_text(plan))
        return 0
    if arguments.command == "render-plan":
        plan = _load_plan(arguments.plan_file, graph, config)
        if arguments.format == "json":
            print(render_plan_json(plan))
        elif arguments.format == "text":
            print(render_plan_text(plan))
        else:
            if plan.mode is BuildMode.LOCAL:
                raise PlatformImagesError("GitLab rendering requires a CI plan")
            registry = environment.get(config.registry.registry_environment_variable)
            if not registry:
                raise PlatformImagesError("registry is required for GitLab rendering")
            print(render_gitlab(plan, config, registry), end="")
        return 0
    if arguments.command == "ci-build":
        result = execute_ci_build(
            graph,
            arguments.name,
            arguments.output_ref,
            parse_input_references(arguments.input_ref),
            root=root,
            environment=environment,
            engine=_engine(arguments, config),
        )
        print(result_json(result))
        return 0
    if arguments.command == "registry-login":
        registry = environment.get(config.registry.registry_environment_variable)
        if not registry:
            raise PlatformImagesError(
                f"{config.registry.registry_environment_variable} is required for registry login"
            )
        engine = _engine(arguments, config)
        login_to_ecr(registry, cwd=root, engine=engine)
        print(
            json.dumps(
                {"registry": registry.rstrip("/"), "authenticated": True, "engine": engine.value}
            )
        )
        return 0
    if arguments.command == "promote":
        ContainerRegistryClient(root, engine=_engine(arguments, config)).promote(
            arguments.source, arguments.destination
        )
        print(
            json.dumps(
                {"source": arguments.source, "destination": arguments.destination},
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "build-plan-target":
        plan = _load_plan(arguments.plan_file, graph, config)
        if plan.mode is BuildMode.LOCAL:
            raise PlatformImagesError("build-plan-target requires a CI plan")
        target = next((target for target in plan.targets if target.name == arguments.name), None)
        if target is None:
            raise PlatformImagesError(f"target is not present in persisted plan: {arguments.name}")
        result = execute_ci_build(
            graph,
            target.name,
            target.output_ref,
            target.input_refs,
            root=root,
            environment=environment,
            engine=_engine(arguments, config),
        )
        print(result_json(result))
        return 0
    if arguments.command == "promote-plan":
        plan = _load_plan(arguments.plan_file, graph, config)
        if plan.mode is not BuildMode.DEFAULT_BRANCH:
            raise PlatformImagesError("promote-plan requires a default-branch CI plan")
        registry = environment.get(config.registry.registry_environment_variable)
        if not registry:
            raise PlatformImagesError(
                f"{config.registry.registry_environment_variable} is required for promotion"
            )
        transport = ContainerRegistryClient(root, engine=_engine(arguments, config))
        policy = ReferencePolicy(config, registry)
        for target in plan.targets:
            transport.promote(target.output_ref, policy.stable(target.name))
        print(json.dumps({"promoted": [target.name for target in plan.targets]}))
        return 0
    if arguments.command == "github-matrix":
        plan = _load_plan(arguments.plan_file, graph, config)
        if plan.mode is BuildMode.LOCAL:
            raise PlatformImagesError("github-matrix requires a CI plan")
        print(render_github_outputs(plan, arguments.max_layers))
        return 0
    if arguments.command == "generate-workflow":
        rendered = render_github_workflow(
            graph,
            config,
            default_branch=arguments.default_branch,
            runner=arguments.runner,
            engine=BuildEngine(arguments.engine),
            aws_auth=arguments.aws_auth,
            aws_role_variable=arguments.aws_role_variable,
            aws_region_variable=arguments.aws_region_variable,
        )
        if arguments.output is None:
            print(rendered, end="")
        else:
            output = arguments.output
            if not output.is_absolute():
                output = root / output
            try:
                output.resolve().relative_to(root)
            except ValueError as exc:
                raise PlatformImagesError(
                    "workflow output must stay within the repository"
                ) from exc
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
            print(output.relative_to(root).as_posix())
        return 0
    raise AssertionError(f"unhandled command: {arguments.command}")


def main(
    argv: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return _run(arguments, cwd, os.environ if environment is None else environment)
    except (PlatformImagesError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
