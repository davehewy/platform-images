from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from platform_images import __version__
from platform_images.backends import default_transport, validate_execution_pair
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
from platform_images.errors import PlatformImagesError
from platform_images.initialization import initialize_repository
from platform_images.manifests import (
    build_manifest_data,
    load_build_manifest,
    manifest_promotions,
    write_json_file,
)
from platform_images.models import (
    BuildBackend,
    BuildMode,
    BuildPlan,
    ChangeSet,
    RegistryTransport,
)
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

CONTAINER_CLI_CHOICES = tuple(
    item.value
    for item in (
        BuildBackend.DOCKER,
        BuildBackend.PODMAN,
        BuildBackend.BUILDAH,
        BuildBackend.NERDCTL,
    )
)


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=34, width=100)


class _ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("formatter_class", _HelpFormatter)
        super().__init__(*args, **kwargs)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="platform",
        description=(
            "Discover container-image dependencies, rebuild the affected DAG, and generate "
            "dependency-safe CI workflows."
        ),
        epilog="""\
examples:
  platform init
  platform images validate
  platform images graph
  platform images build api --dry-run
  platform version

Run 'platform COMMAND --help' for command-specific help.
""",
    )
    parser.add_argument("--version", action="version", version=f"platform-images {__version__}")
    root_subparsers = parser.add_subparsers(
        title="commands", dest="group", metavar="COMMAND", required=True
    )

    init = root_subparsers.add_parser(
        "init",
        help="create a safe starter configuration from the repository layout",
        description=(
            "Create platform-images.toml without overwriting an existing configuration. By "
            "default, target groups are inferred from existing Dockerfile and Containerfile "
            "locations."
        ),
        epilog="""\
examples:
  platform init
  platform init --discovery-root containers/shared --discovery-root services/api/images
  platform init --namespace my-team/my-repository --builder podman
""",
    )
    init.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root (default: current directory or PLATFORM_IMAGES_ROOT)",
    )
    init.add_argument(
        "--discovery-root",
        dest="discovery_roots",
        action="append",
        default=[],
        metavar="PATH",
        help="target parent directory; repeat for multiple roots (default: infer existing roots)",
    )
    init.add_argument(
        "--namespace",
        help="registry repository namespace (default: a normalized repository directory name)",
    )
    init.add_argument(
        "--builder",
        choices=CONTAINER_CLI_CHOICES,
        default=BuildBackend.DOCKER.value,
        help="default local and CI build backend (default: docker)",
    )
    init.add_argument(
        "--registry-transport",
        choices=CONTAINER_CLI_CHOICES,
        help="registry client (default: match --builder)",
    )

    images = root_subparsers.add_parser(
        "images",
        help="inspect, plan, build, and publish repository image targets",
        description=(
            "Inspect the repository image DAG, calculate change-aware build plans, execute exact-"
            "reference builds, and render CI automation."
        ),
        epilog="""\
examples:
  platform images list
  platform images show api
  platform images graph --format json
  platform images affected --base origin/main --head HEAD
  platform images plan --ci --format json

Run 'platform images COMMAND --help' for command-specific options.
""",
    )
    images.add_argument(
        "--root",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    commands = images.add_subparsers(
        title="image commands", dest="command", metavar="COMMAND", required=True
    )

    commands.add_parser(
        "list",
        help="list every discovered image target",
        description="List every discovered image target.",
    )

    show = commands.add_parser(
        "show",
        help="show one target's build file, dependencies, and dependents",
        description="Show one target's build file, direct dependencies, and direct dependents.",
    )
    show.add_argument("name")
    show.add_argument("--format", choices=("text", "json"), default="text")

    validate = commands.add_parser(
        "validate",
        help="validate configuration, target discovery, references, and DAG safety",
        description="Validate configuration, target discovery, references, and DAG safety.",
    )
    validate.add_argument("--format", choices=("text", "json"), default="text")

    graph = commands.add_parser(
        "graph",
        help="print the complete container-image dependency DAG",
        description="Print the complete container-image dependency DAG.",
    )
    graph.add_argument("--format", choices=("text", "json"), default="text")
    graph.add_argument(
        "--ascii",
        action="store_true",
        help="use portable ASCII tree connectors instead of Unicode line drawing",
    )

    build = commands.add_parser(
        "build",
        help="build a local target after its required local dependencies",
        description="Build a local target after its required local dependencies.",
    )
    build.add_argument("name")
    build.add_argument("--dry-run", action="store_true")
    build.add_argument("--no-deps", action="store_true")
    build.add_argument("--builder", choices=CONTAINER_CLI_CHOICES)
    build.add_argument(
        "--engine",
        choices=CONTAINER_CLI_CHOICES,
        help="backward-compatible shorthand for --builder",
    )

    for name, summary in (
        ("changed", "list targets changed directly between two Git revisions"),
        ("affected", "list changed targets and all of their downstream consumers"),
    ):
        command = commands.add_parser(name, help=summary, description=summary.capitalize() + ".")
        command.add_argument("--base", required=True)
        command.add_argument("--head", required=True)
        command.add_argument("--format", choices=("text", "json"), default="text")

    plan = commands.add_parser(
        "plan",
        help="calculate an ordered local or CI build plan with exact image references",
        description="Calculate an ordered local or CI build plan with exact image references.",
    )
    plan.add_argument("--all", action="store_true")
    plan.add_argument("--image", action="append", default=[])
    plan.add_argument("--base")
    plan.add_argument("--head")
    plan.add_argument("--ci", action="store_true")
    plan.add_argument("--format", choices=("text", "json", "gitlab"), default="text")

    render_plan = commands.add_parser(
        "render-plan",
        help="validate and render a previously persisted build plan",
        description="Validate and render a previously persisted build plan.",
    )
    render_plan.add_argument("plan_file", type=Path)
    render_plan.add_argument("--format", choices=("text", "json", "gitlab"), default="gitlab")

    ci_build = commands.add_parser(
        "ci-build",
        help="build and push one CI target using explicit input and output references",
        description="Build and push one CI target using explicit input and output references.",
    )
    ci_build.add_argument("name")
    ci_build.add_argument("--output-ref", required=True)
    ci_build.add_argument("--input-ref", action="append", default=[])
    ci_build.add_argument(
        "--result-file",
        type=Path,
        help="write the pushed image identity and digest as JSON",
    )
    _execution_arguments(ci_build)

    registry_login = commands.add_parser(
        "registry-login",
        help="authenticate the selected registry transport to ECR",
        description="Authenticate the selected registry transport to ECR.",
    )
    _transport_arguments(registry_login)

    promote_parser = commands.add_parser(
        "promote",
        help="copy an existing image reference to a new registry tag",
        description="Copy an existing image reference to a new registry tag.",
    )
    promote_parser.add_argument("--source", required=True)
    promote_parser.add_argument("--destination", required=True)
    _transport_arguments(promote_parser)

    build_plan_target = commands.add_parser(
        "build-plan-target",
        help="build one target exactly as defined by a persisted CI plan",
        description="Build one target exactly as defined by a persisted CI plan.",
    )
    build_plan_target.add_argument("plan_file", type=Path)
    build_plan_target.add_argument("name")
    build_plan_target.add_argument(
        "--result-file",
        type=Path,
        help="write the pushed image identity and digest as JSON",
    )
    _execution_arguments(build_plan_target)

    build_manifest = commands.add_parser(
        "build-manifest",
        help="verify build results and publish a commit-to-image manifest",
        description="Verify build results and publish a commit-to-image manifest.",
    )
    build_manifest.add_argument(
        "result_paths",
        nargs="*",
        type=Path,
        help="result JSON files or directories containing them",
    )
    build_manifest.add_argument("--plan", type=Path, help="authoritative CI plan to verify")
    build_manifest.add_argument(
        "--mode",
        choices=(BuildMode.MERGE_REQUEST.value, BuildMode.DEFAULT_BRANCH.value),
    )
    build_manifest.add_argument("--base-sha")
    build_manifest.add_argument("--commit-sha")
    build_manifest.add_argument("--source")
    build_manifest.add_argument("--expected-target", action="append", default=[])
    build_manifest.add_argument("--output", type=Path, help="write the manifest instead of stdout")

    promote_manifest = commands.add_parser(
        "promote-manifest",
        help="promote tested manifest digests to a release tag without rebuilding",
        description="Promote tested manifest digests to a release tag without rebuilding.",
    )
    promote_manifest.add_argument("manifest_file", type=Path)
    promote_manifest.add_argument(
        "--tag", required=True, help="destination tag, for example v1.2.3"
    )
    promote_manifest.add_argument(
        "--expected-commit",
        help="required source commit; defaults to CI_COMMIT_SHA",
    )
    promote_manifest.add_argument(
        "--image",
        action="append",
        default=[],
        help="promote only this image; repeat to select several",
    )
    _transport_arguments(promote_manifest)

    promote_plan = commands.add_parser(
        "promote-plan",
        help="promote every output in a successful default-branch plan to stable",
        description="Promote every output in a successful default-branch plan to stable.",
    )
    promote_plan.add_argument("plan_file", type=Path)
    _transport_arguments(promote_plan)

    github_matrix = commands.add_parser(
        "github-matrix",
        help="render dependency-layered GitHub matrix outputs from a CI plan",
        description="Render dependency-layered GitHub matrix outputs from a CI plan.",
    )
    github_matrix.add_argument("plan_file", type=Path)
    github_matrix.add_argument("--max-layers", type=int, required=True)

    workflow = commands.add_parser(
        "generate-workflow",
        help="generate a complete dependency-aware CI workflow",
        description="Generate a complete dependency-aware CI workflow.",
    )
    workflow.add_argument("provider", choices=("github",))
    workflow.add_argument("--default-branch", default="main")
    workflow.add_argument("--runner", default="ubuntu-latest")
    _execution_arguments(workflow)
    workflow.add_argument("--aws-auth", choices=("oidc", "ambient"), default="oidc")
    workflow.add_argument("--aws-role-variable", default="AWS_ROLE_TO_ASSUME")
    workflow.add_argument("--aws-region-variable", default="AWS_REGION")
    workflow.add_argument("--output", type=Path)

    version = root_subparsers.add_parser(
        "version",
        help="print the installed platform-images version",
        description="Print the installed platform-images version.",
    )
    version.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    return parser


def _execution_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--builder", choices=CONTAINER_CLI_CHOICES)
    parser.add_argument(
        "--registry-transport",
        choices=CONTAINER_CLI_CHOICES,
    )
    parser.add_argument(
        "--engine",
        choices=CONTAINER_CLI_CHOICES,
        help="backward-compatible shorthand selecting a matching builder and transport",
    )


def _transport_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--registry-transport",
        choices=CONTAINER_CLI_CHOICES,
    )
    parser.add_argument(
        "--engine",
        choices=CONTAINER_CLI_CHOICES,
        help="backward-compatible shorthand for --registry-transport",
    )


def _root(arguments: argparse.Namespace, cwd: Path | None) -> Path:
    configured = getattr(arguments, "root", None) or os.environ.get("PLATFORM_IMAGES_ROOT")
    return Path(configured).resolve() if configured else (cwd or Path.cwd()).resolve()


def _run_init(arguments: argparse.Namespace, cwd: Path | None) -> int:
    root = _root(arguments, cwd)
    result = initialize_repository(
        root,
        discovery_roots=tuple(arguments.discovery_roots),
        namespace=arguments.namespace,
        builder=BuildBackend(arguments.builder),
        transport=(
            RegistryTransport(arguments.registry_transport)
            if arguments.registry_transport is not None
            else None
        ),
    )
    report = validate_repository(RepositoryConfig.load(root))
    relative_configuration = result.configuration_path.relative_to(root).as_posix()
    target_word = "target" if result.target_count == 1 else "targets"
    root_word = "root" if len(result.discovery_roots) == 1 else "roots"
    print(f"Created {relative_configuration}")
    print(
        f"Discovered {result.target_count} image {target_word} across "
        f"{len(result.discovery_roots)} {root_word}:"
    )
    for discovery_root in result.discovery_roots:
        print(f"  - {discovery_root}")
    if result.allowed_short_external_images:
        print("Allowed short external images inferred from build files:")
        for image in result.allowed_short_external_images:
            print(f"  - {image}")
    if report.valid:
        print(f"Initial validation passed ({result.target_count} image {target_word}).")
        print("Next: platform images graph")
    else:
        error_word = "error" if len(report.errors) == 1 else "errors"
        print(f"Initial validation found {len(report.errors)} {error_word}.")
        print("Next: platform images validate")
    return 0


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


def _builder(
    arguments: argparse.Namespace,
    config: RepositoryConfig,
    *,
    default: BuildBackend | None = None,
) -> BuildBackend:
    selected = getattr(arguments, "builder", None)
    legacy = getattr(arguments, "engine", None)
    if selected is not None and legacy is not None and selected != legacy:
        raise PlatformImagesError("--builder and --engine select different build backends")
    if selected or legacy:
        return BuildBackend(selected or legacy)
    return default or config.build.backend


def _transport(
    arguments: argparse.Namespace,
    config: RepositoryConfig,
    *,
    default: RegistryTransport | None = None,
) -> RegistryTransport:
    selected = getattr(arguments, "registry_transport", None)
    legacy = getattr(arguments, "engine", None)
    if selected is not None and legacy is not None and selected != legacy:
        raise PlatformImagesError(
            "--registry-transport and --engine select different registry transports"
        )
    if selected or legacy:
        return RegistryTransport(selected or legacy)
    return default or config.registry.transport


def _execution(
    arguments: argparse.Namespace,
    config: RepositoryConfig,
    *,
    default_builder: BuildBackend | None = None,
) -> tuple[BuildBackend, RegistryTransport]:
    builder = _builder(arguments, config, default=default_builder)
    no_overrides = not any(
        getattr(arguments, name, None) for name in ("builder", "registry_transport", "engine")
    )
    transport_default = (
        default_transport(builder) if default_builder is not None and no_overrides else None
    )
    transport = _transport(
        arguments,
        config,
        default=transport_default,
    )
    validate_execution_pair(builder, transport)
    return builder, transport


def _load_plan(path: Path, graph, config: RepositoryConfig) -> BuildPlan:
    try:
        plan = load_plan_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlatformImagesError(f"unable to read persisted plan {path}: {exc}") from exc
    validate_plan_against_graph(plan, graph, config)
    return plan


def _artifact_path(path: Path, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def _emit_build_result(result: Mapping[str, object], path: Path | None, *, root: Path) -> None:
    if path is not None:
        write_json_file(_artifact_path(path, root), result)
    print(result_json(result))


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
    if arguments.group == "version":
        if arguments.format == "json":
            print(json.dumps({"name": "platform-images", "version": __version__}, sort_keys=True))
        else:
            print(f"platform-images {__version__}")
        return 0
    if arguments.group == "init":
        return _run_init(arguments, cwd)

    root = _root(arguments, cwd)
    config = RepositoryConfig.load(root)
    report = validate_repository(config)
    if arguments.command == "validate":
        print(_render_validation(report, arguments.format))
        return 0 if report.valid else 1
    _require_valid(report)
    graph = report.graph

    if arguments.command == "list":
        print("\n".join(sorted(graph.targets)))
        return 0

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
            else render_graph_text(graph, ascii_only=arguments.ascii or sys.platform == "win32")
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
            builder=_builder(arguments, config),
        )
        if arguments.dry_run:
            print("\n".join(commands))
        return 0
    if arguments.command in {"changed", "affected"}:
        changes = detect_changes(config, graph.targets, arguments.base, arguments.head)
        validate_removed_references(graph, changes, config.root)
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
        builder, transport = _execution(arguments, config)
        result = execute_ci_build(
            graph,
            arguments.name,
            arguments.output_ref,
            parse_input_references(arguments.input_ref),
            root=root,
            environment=environment,
            builder=builder,
            registry_transport=transport,
        )
        _emit_build_result(result, arguments.result_file, root=root)
        return 0
    if arguments.command == "registry-login":
        registry = environment.get(config.registry.registry_environment_variable)
        if not registry:
            raise PlatformImagesError(
                f"{config.registry.registry_environment_variable} is required for registry login"
            )
        transport = _transport(arguments, config)
        login_to_ecr(registry, cwd=root, transport=transport)
        print(
            json.dumps(
                {
                    "registry": registry.rstrip("/"),
                    "authenticated": True,
                    "engine": transport.value,
                    "registry_transport": transport.value,
                }
            )
        )
        return 0
    if arguments.command == "promote":
        ContainerRegistryClient(root, transport=_transport(arguments, config)).promote(
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
        builder, transport = _execution(arguments, config)
        result = execute_ci_build(
            graph,
            target.name,
            target.output_ref,
            target.input_refs,
            root=root,
            environment=environment,
            builder=builder,
            registry_transport=transport,
        )
        _emit_build_result(result, arguments.result_file, root=root)
        return 0
    if arguments.command == "build-manifest":
        plan = (
            _load_plan(_artifact_path(arguments.plan, root), graph, config)
            if arguments.plan is not None
            else None
        )
        mode = BuildMode(arguments.mode) if arguments.mode is not None else None
        manifest = build_manifest_data(
            (_artifact_path(path, root) for path in arguments.result_paths),
            plan=plan,
            mode=mode,
            base_sha=arguments.base_sha,
            commit_sha=arguments.commit_sha or environment.get("CI_COMMIT_SHA"),
            source=arguments.source or environment.get("CI_PROJECT_URL"),
            expected_targets=arguments.expected_target,
        )
        if arguments.output is not None:
            write_json_file(_artifact_path(arguments.output, root), manifest)
            print(arguments.output)
        else:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    if arguments.command == "promote-manifest":
        expected_commit = arguments.expected_commit or environment.get("CI_COMMIT_SHA")
        if not expected_commit:
            raise PlatformImagesError(
                "CI_COMMIT_SHA or --expected-commit is required for manifest promotion"
            )
        manifest = load_build_manifest(_artifact_path(arguments.manifest_file, root))
        promotions = manifest_promotions(
            manifest,
            arguments.tag,
            expected_commit=expected_commit,
            selected_images=arguments.image,
        )
        transport = ContainerRegistryClient(root, transport=_transport(arguments, config))
        for promotion in promotions:
            transport.promote(promotion["source"], promotion["destination"])
        print(json.dumps({"commit_sha": expected_commit, "promoted": promotions}, sort_keys=True))
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
        transport = ContainerRegistryClient(root, transport=_transport(arguments, config))
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
        builder, transport = _execution(
            arguments,
            config,
            default_builder=BuildBackend.DOCKER,
        )
        rendered = render_github_workflow(
            graph,
            config,
            default_branch=arguments.default_branch,
            runner=arguments.runner,
            builder=builder,
            registry_transport=transport,
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
