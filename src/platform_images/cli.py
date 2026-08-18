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
    parse_build_arguments,
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
from platform_images.initialization import (
    AmbiguousRepository,
    RegistryAdoption,
    RepositoryInference,
    initialize_repository,
    reconcile_repository,
)
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
    RegistryAuthentication,
    RegistryProvider,
    RegistryTransport,
)
from platform_images.normalization import normalize_internal_references
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
    OCIRegistryClient,
    login_to_registry,
    registry_from_environment_json,
)
from platform_images.renderers.bake import render_bake
from platform_images.renderers.github import render_github_outputs, render_github_workflow
from platform_images.renderers.gitlab import GITLAB_DEFAULT_JOB_LIMIT, render_gitlab
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
  platform reconcile
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
            "locations, qualified repositories are paired with likely local targets, and the "
            "smallest cascading repository policy is written with a review audit."
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
        help=(
            "global repository namespace; cascades to every target and overrides inference "
            "(default: infer from local references, then repository directory name)"
        ),
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
    init.add_argument(
        "--registry-provider",
        choices=tuple(provider.value for provider in RegistryProvider),
        default=RegistryProvider.OCI.value,
        help="stable-tag registry API (default: oci; use ecr for AWS-native lookup)",
    )
    init.add_argument(
        "--registry-authentication",
        choices=tuple(authentication.value for authentication in RegistryAuthentication),
        help="registry login policy (default: credentials for oci, ecr for ecr)",
    )
    init.add_argument(
        "--registry-username-variable",
        default="PLATFORM_IMAGES_REGISTRY_USERNAME",
        help="environment variable containing an OCI registry username",
    )
    init.add_argument(
        "--registry-password-variable",
        default="PLATFORM_IMAGES_REGISTRY_PASSWORD",
        help="environment variable containing an OCI registry password or token",
    )
    init.add_argument(
        "--build-arg",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="checked-in Dockerfile ARG value used for graphing and builds; repeat as needed",
    )
    init_mode = init.add_mutually_exclusive_group()
    init_mode.add_argument(
        "--interactive",
        action="store_true",
        help="review inferred registries, mappings, and ambiguous references step by step",
    )
    init_mode.add_argument(
        "--yes",
        action="store_true",
        help="accept every high-confidence recommendation without prompting (the default)",
    )
    init_mode.add_argument(
        "--check",
        action="store_true",
        help="calculate and validate the recommended configuration without writing it",
    )
    init.add_argument(
        "--report-json",
        type=Path,
        help="write the complete adoption evidence and validation result as JSON",
    )

    reconcile = root_subparsers.add_parser(
        "reconcile",
        help="update existing configuration from high-confidence repository evidence",
        description=(
            "Reconcile platform-images.toml with newly discovered external bases and qualified "
            "references. Existing output policy remains authoritative, manual settings are "
            "preserved, and only unique high-confidence additions are written."
        ),
        epilog="""\
examples:
  platform reconcile
  platform reconcile --check
""",
    )
    reconcile.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repository root (default: current directory or PLATFORM_IMAGES_ROOT)",
    )
    reconcile.add_argument(
        "--check",
        action="store_true",
        help="print the proposed diff without writing and fail when updates are available",
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
  platform images generate-bake --all --output docker-bake.hcl

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

    normalize = commands.add_parser(
        "normalize-references",
        help="preview or apply qualified internal references as short logical target names",
        description=(
            "Rewrite only registry-qualified image operands that already resolve to local targets "
            "through identity.internal_registries. The default is a non-writing diff preview."
        ),
        epilog="""\
examples:
  platform images normalize-references
  platform images normalize-references --check
  platform images normalize-references --apply
""",
    )
    normalize_mode = normalize.add_mutually_exclusive_group()
    normalize_mode.add_argument(
        "--apply",
        action="store_true",
        help="apply the displayed changes atomically and revalidate the repository",
    )
    normalize_mode.add_argument(
        "--check",
        action="store_true",
        help="do not write and return a failure status when normalization is available",
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
    plan.add_argument(
        "--gitlab-max-jobs",
        type=int,
        default=GITLAB_DEFAULT_JOB_LIMIT,
        help=(f"configured GitLab per-pipeline job limit (default: {GITLAB_DEFAULT_JOB_LIMIT})"),
    )

    bake = commands.add_parser(
        "generate-bake",
        help="generate a dependency-aware Docker Buildx Bake definition",
        description=(
            "Generate deterministic Docker Buildx Bake HCL from an authoritative local, change, "
            "CI, or persisted build plan. With no selector, every discovered image is included."
        ),
        epilog="""\
examples:
  platform images generate-bake --output docker-bake.hcl
  platform images generate-bake --image api --output docker-bake.hcl
  platform images generate-bake --ci --output docker-bake.hcl
  platform images generate-bake --plan image-plan.json --output docker-bake.hcl

Run 'docker buildx bake --print' to inspect, '--load' for local images, or '--push' in CI.
""",
    )
    bake.add_argument("--all", action="store_true", help="include every discovered image")
    bake.add_argument(
        "--image",
        action="append",
        default=[],
        help="include this image and its local dependencies; repeat to select several",
    )
    bake.add_argument("--base", help="base Git revision for a change-aware CI plan")
    bake.add_argument("--head", help="head Git revision for a change-aware CI plan")
    bake.add_argument(
        "--ci",
        action="store_true",
        help="derive the affected CI plan from CI variables",
    )
    bake.add_argument(
        "--plan",
        dest="plan_file",
        type=Path,
        help="render an existing authoritative plan JSON instead of calculating one",
    )
    bake.add_argument(
        "--output",
        type=Path,
        help="write HCL inside the repository instead of printing it",
    )

    render_plan = commands.add_parser(
        "render-plan",
        help="validate and render a previously persisted build plan",
        description="Validate and render a previously persisted build plan.",
    )
    render_plan.add_argument("plan_file", type=Path)
    render_plan.add_argument("--format", choices=("text", "json", "gitlab"), default="gitlab")
    render_plan.add_argument(
        "--gitlab-max-jobs",
        type=int,
        default=GITLAB_DEFAULT_JOB_LIMIT,
        help=(f"configured GitLab per-pipeline job limit (default: {GITLAB_DEFAULT_JOB_LIMIT})"),
    )

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
        help="authenticate the selected transport to the configured registry provider",
        description=(
            "Authenticate with ECR, use OCI username/password credentials, or deliberately rely "
            "on ambient transport credentials according to platform-images.toml."
        ),
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
    github_matrix.add_argument(
        "--max-layers",
        type=int,
        required=True,
        help="static dependency layers available in the generated workflow",
    )
    github_matrix.add_argument(
        "--max-shards",
        type=int,
        default=1,
        help="parallel matrix jobs available per layer (default: 1)",
    )

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


def _confirm(prompt: str, *, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        try:
            answer = input(prompt + suffix).strip().casefold()
        except (EOFError, KeyboardInterrupt) as exc:
            raise PlatformImagesError("interactive initialization was cancelled") from exc
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def _review_registry(adoption: RegistryAdoption) -> bool:
    print()
    print(f"Probable internal registry: {adoption.registry}")
    print(
        f"  {adoption.local_reference_count}/{adoption.reference_count} qualified references "
        f"match {adoption.local_target_count} discovered local targets"
    )
    if adoption.unmatched_reference_count:
        print(f"  {adoption.unmatched_reference_count} references remain external or need review")
    if adoption.managed_prefixes:
        print("  Recommended managed repository prefixes:")
        for prefix in adoption.managed_prefixes:
            print(f"    - {prefix}")
    return _confirm("Accept this grouped registry recommendation?")


def _review_inferences(inferences: tuple[RepositoryInference, ...]) -> bool:
    print()
    print(f"High-confidence local repository mappings ({len(inferences)}):")
    for inference in inferences:
        print(
            f"  - {inference.source_repository} -> {inference.target} "
            f"({inference.confidence}; {inference.reference_count} references)"
        )
    return _confirm("Apply these mappings?")


def _review_ambiguity(ambiguity: AmbiguousRepository) -> str | None:
    print()
    print(
        f"Qualified repository needing review ({ambiguity.reference_count} references):\n"
        f"  {ambiguity.repository}"
    )
    for index, target in enumerate(ambiguity.candidates, 1):
        print(f"  {index}. Map to local target {target}")
    external_index = len(ambiguity.candidates) + 1
    unresolved_index = external_index + 1
    print(f"  {external_index}. Treat as external")
    print(f"  {unresolved_index}. Leave unresolved for later review")
    while True:
        try:
            answer = input(
                f"Selection [1-{unresolved_index}, default {unresolved_index}]: "
            ).strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise PlatformImagesError("interactive initialization was cancelled") from exc
        if not answer:
            return None
        try:
            selected = int(answer)
        except ValueError:
            print("Enter one of the numbered choices.")
            continue
        if 1 <= selected <= len(ambiguity.candidates):
            return ambiguity.candidates[selected - 1]
        if selected == external_index:
            return "external"
        if selected == unresolved_index:
            return None
        print("Enter one of the numbered choices.")


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
        registry_provider=RegistryProvider(arguments.registry_provider),
        registry_authentication=(
            RegistryAuthentication(arguments.registry_authentication)
            if arguments.registry_authentication is not None
            else None
        ),
        registry_username_environment_variable=arguments.registry_username_variable,
        registry_password_environment_variable=arguments.registry_password_variable,
        build_arguments=parse_build_arguments(arguments.build_arg),
        registry_decider=_review_registry if arguments.interactive else None,
        inference_decider=_review_inferences if arguments.interactive else None,
        ambiguous_decider=_review_ambiguity if arguments.interactive else None,
        write=not arguments.check,
    )
    report = result.validation_report
    relative_configuration = result.configuration_path.relative_to(root).as_posix()
    target_word = "target" if result.target_count == 1 else "targets"
    root_word = "root" if len(result.discovery_roots) == 1 else "roots"
    print(
        f"Would create {relative_configuration}"
        if arguments.check
        else f"Created {relative_configuration}"
    )
    print(
        f"Discovered {result.target_count} image {target_word} across "
        f"{len(result.discovery_roots)} {root_word}:"
    )
    for discovery_root in result.discovery_roots:
        print(f"  - {discovery_root}")
    if result.inferred_registry_namespace is not None:
        print(
            "Output registry namespace inferred from qualified local references: "
            f"{result.inferred_registry_namespace}"
        )
    elif result.source_repository_namespaces:
        print(
            "Qualified local-reference namespaces detected: "
            + ", ".join(result.source_repository_namespaces)
        )
        print(
            "They resolve automatically; build outputs use registry namespace: "
            f"{result.registry_namespace}"
        )
    if result.registry_adoptions:
        print("Internal registry adoption summary:")
        for adoption in result.registry_adoptions:
            print(
                f"  - {adoption.registry}: {adoption.local_reference_count}/"
                f"{adoption.reference_count} references matched "
                f"{adoption.local_target_count} local targets"
            )
            for prefix in adoption.managed_prefixes:
                print(f"    managed prefix: {prefix}")
    if result.repository_inferences:
        guess_word = "mapping" if len(result.repository_inferences) == 1 else "mappings"
        print(
            f"Review warning: init applied {len(result.repository_inferences)} inferred repository "
            f"{guess_word}:"
        )
        for inference in result.repository_inferences:
            reference_word = "reference" if inference.reference_count == 1 else "references"
            print(
                f"  - {inference.source_repository} -> {inference.target} "
                f"({inference.confidence}, {inference.reference_count} {reference_word}; "
                f"{inference.action})"
            )
        print(
            "The generated global namespace cascades to every target; only naming exceptions "
            "were written under [images]."
        )
    if result.allowed_short_external_images:
        print("Allowed short external images inferred from build files:")
        for image in result.allowed_short_external_images:
            print(f"  - {image}")
    if result.external_repositories:
        print("Confirmed external repositories:")
        for repository in result.external_repositories:
            print(f"  - {repository}")
    if result.ambiguous_repositories:
        count = len(result.ambiguous_repositories)
        print(f"Review required for {count} ambiguous qualified repositories:")
        for ambiguity in result.ambiguous_repositories:
            print(f"  - {ambiguity.repository} -> " + ", ".join(ambiguity.candidates))
        print("Run 'platform reconcile' after adding an explicit alias or external exception.")
    if report.valid:
        print(f"Initial validation passed ({result.target_count} image {target_word}).")
        if report.warnings:
            _summary, _separator, warning_details = _render_validation(report, "text").partition(
                "\n\n"
            )
            print()
            print(warning_details)
        print("Next: platform images graph")
    else:
        error_word = "error" if len(report.errors) == 1 else "errors"
        print(f"Initial validation found {len(report.errors)} {error_word}.")
        print("Next: platform images validate")
    if arguments.report_json is not None:
        report_path = _artifact_path(arguments.report_json, root)
        try:
            report_path.resolve().relative_to(root)
        except ValueError as exc:
            raise PlatformImagesError("init report output must stay within the repository") from exc
        write_json_file(
            report_path,
            {
                "schema_version": 1,
                "configuration": relative_configuration,
                "target_count": result.target_count,
                "discovery_roots": list(result.discovery_roots),
                "registry_namespace": result.registry_namespace,
                "registry_adoptions": [
                    {
                        "registry": adoption.registry,
                        "managed_prefixes": list(adoption.managed_prefixes),
                        "reference_count": adoption.reference_count,
                        "local_reference_count": adoption.local_reference_count,
                        "local_target_count": adoption.local_target_count,
                        "unmatched_reference_count": adoption.unmatched_reference_count,
                    }
                    for adoption in result.registry_adoptions
                ],
                "repository_mappings": [
                    {
                        "target": inference.target,
                        "source_repository": inference.source_repository,
                        "action": inference.action,
                        "confidence": inference.confidence,
                        "reference_count": inference.reference_count,
                    }
                    for inference in result.repository_inferences
                ],
                "ambiguous_repositories": [
                    {
                        "repository": ambiguity.repository,
                        "normalized_repository": ambiguity.normalized_repository,
                        "candidates": list(ambiguity.candidates),
                        "reference_count": ambiguity.reference_count,
                    }
                    for ambiguity in result.ambiguous_repositories
                ],
                "validation": {
                    "valid": report.valid,
                    "errors": [issue.code for issue in report.errors],
                    "warnings": [issue.code for issue in report.warnings],
                },
            },
        )
        print(f"Wrote adoption report: {report_path.relative_to(root).as_posix()}")
    if arguments.check:
        print("Run 'platform init' to write this validated recommendation.")
        return 1
    return 0


def _run_reconcile(arguments: argparse.Namespace, cwd: Path | None) -> int:
    root = _root(arguments, cwd)
    result = reconcile_repository(root, write=not arguments.check)
    relative_configuration = result.configuration_path.relative_to(root).as_posix()

    if result.changed:
        if arguments.check:
            print(f"Configuration reconciliation required: {relative_configuration}")
        else:
            print(f"Updated {relative_configuration}")
        if result.added_short_external_images:
            print("Added unambiguous short external images:")
            for image in result.added_short_external_images:
                print(f"  - {image}")
        if result.repository_inferences:
            mapping_word = "mapping" if len(result.repository_inferences) == 1 else "mappings"
            print(
                f"Applied {len(result.repository_inferences)} high-confidence repository "
                f"{mapping_word}:"
            )
            for inference in result.repository_inferences:
                reference_word = "reference" if inference.reference_count == 1 else "references"
                print(
                    f"  - {inference.source_repository} -> {inference.target} "
                    f"({inference.confidence}, {inference.reference_count} {reference_word}; "
                    f"{inference.action})"
                )
        if result.registry_adoptions:
            print("Added grouped internal registry policy:")
            for adoption in result.registry_adoptions:
                print(
                    f"  - {adoption.registry}: {adoption.local_reference_count}/"
                    f"{adoption.reference_count} references matched "
                    f"{adoption.local_target_count} local targets"
                )
                for prefix in adoption.managed_prefixes:
                    print(f"    managed prefix: {prefix}")
        print()
        print(result.diff, end="" if result.diff.endswith("\n") else "\n")
        if arguments.check:
            print("Run 'platform reconcile' to apply these exact updates.")
            return 1
    else:
        print(f"Configuration already reconciled: {relative_configuration}")

    report = result.validation_report
    if report.valid:
        target_word = "target" if result.target_count == 1 else "targets"
        print(f"Validation passed ({result.target_count} image {target_word}).")
        if report.warnings:
            _summary, _separator, warning_details = _render_validation(report, "text").partition(
                "\n\n"
            )
            print()
            print(warning_details)
        return 0

    print(_render_validation(report, "text"), file=sys.stderr)
    return 1


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

    def append_issues(lines: list[str], issues: tuple[ValidationIssue, ...]) -> None:
        for issue in issues:
            location = issue.path or "repository"
            if issue.line is not None:
                location += f":{issue.line}"
            lines.extend(["", location, f"  [{issue.code}] {issue.message}"])
            if issue.hint:
                lines.append(f"  {issue.hint}")

    if report.valid:
        lines = [f"Validation passed ({len(report.graph.targets)} image targets)."]
        if report.warnings:
            lines.extend(["", f"Warnings: {len(report.warnings)}"])
            append_issues(lines, report.warnings)
        return "\n".join(lines)
    lines = [f"Validation failed with {len(report.errors)} errors:"]
    append_issues(lines, report.errors)
    if report.warnings:
        lines.extend(["", f"Warnings: {len(report.warnings)}"])
        append_issues(lines, report.warnings)
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
    if static is not None:
        return static
    if config.registry.provider is RegistryProvider.OCI:
        return OCIRegistryClient(config, registry, env)
    return ECRRegistryClient(config, registry)


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
    *,
    default_all: bool = False,
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
    if selectors == 0 and default_all:
        return local_plan(graph, config, frozenset(graph.targets))
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
    if arguments.group == "reconcile":
        return _run_reconcile(arguments, cwd)

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
        data = graph_data(graph, root, config)["targets"][arguments.name]  # type: ignore[index]
        if arguments.format == "json":
            print(json.dumps({"schema_version": 1, "name": arguments.name, **data}, indent=2))
        else:
            print(arguments.name)
            print(f"  build_file: {data['dockerfile']}")
            print(f"  repository: {data['repository']}")
            print(f"  aliases: {', '.join(data['aliases']) or 'none'}")
            print(f"  dependencies: {', '.join(data['dependencies']) or 'none'}")
            if data["local_references"]:
                print("  local_references:")
                for reference in data["local_references"]:
                    print(
                        f"    {reference['source']} -> {reference['target']} "
                        f"({reference['instruction']}, line {reference['line']})"
                    )
            else:
                print("  local_references: none")
            print(f"  dependents: {', '.join(data['dependents']) or 'none'}")
        return 0
    if arguments.command == "graph":
        print(
            render_graph_json(graph, root, config)
            if arguments.format == "json"
            else render_graph_text(graph, ascii_only=arguments.ascii or sys.platform == "win32")
        )
        return 0
    if arguments.command == "normalize-references":
        result = normalize_internal_references(config, write=arguments.apply)
        if result.changed:
            print(result.diff, end="" if result.diff.endswith("\n") else "\n")
            if arguments.apply:
                file_word = "file" if len(result.files) == 1 else "files"
                print(
                    f"Normalized qualified local references in {len(result.files)} build "
                    f"{file_word}."
                )
                print("Validation passed after normalization.")
            elif arguments.check:
                print("Run 'platform images normalize-references --apply' to apply this diff.")
                return 1
            else:
                print("Preview only; use --apply to write these changes.")
        else:
            print("Internal local references are already normalized.")
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
            build_arguments=config.dockerfile.arguments,
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
            print(
                render_gitlab(
                    plan,
                    config,
                    registry,
                    max_jobs=arguments.gitlab_max_jobs,
                ),
                end="",
            )
        else:
            print(render_plan_text(plan))
        return 0
    if arguments.command == "generate-bake":
        selectors = any(
            (
                arguments.all,
                arguments.image,
                arguments.base,
                arguments.head,
                arguments.ci,
            )
        )
        if arguments.plan_file is not None and selectors:
            raise PlatformImagesError("--plan cannot be combined with another Bake selector")
        plan = (
            _load_plan(_artifact_path(arguments.plan_file, root), graph, config)
            if arguments.plan_file is not None
            else _create_plan(arguments, config, report, environment, default_all=True)
        )
        if plan.mode is not BuildMode.LOCAL:
            group_name = "affected"
        elif arguments.plan_file is not None or arguments.image:
            group_name = "selected"
        else:
            group_name = "all"
        rendered = render_bake(
            plan,
            config,
            group_name=group_name,
            source=environment.get("CI_PROJECT_URL"),
        )
        if arguments.output is None:
            print(rendered, end="")
        else:
            output = _artifact_path(arguments.output, root)
            try:
                relative_output = output.resolve().relative_to(root)
            except ValueError as exc:
                raise PlatformImagesError("Bake output must stay within the repository") from exc
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8", newline="\n")
            print(relative_output.as_posix())
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
            print(
                render_gitlab(
                    plan,
                    config,
                    registry,
                    max_jobs=arguments.gitlab_max_jobs,
                ),
                end="",
            )
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
            build_arguments=config.dockerfile.arguments,
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
        authenticated = login_to_registry(
            config,
            registry,
            environment=environment,
            cwd=root,
            transport=transport,
        )
        print(
            json.dumps(
                {
                    "registry": registry.rstrip("/"),
                    "authenticated": authenticated,
                    "authentication": config.registry.authentication.value,
                    "provider": config.registry.provider.value,
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
            build_arguments=config.dockerfile.arguments,
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
        print(render_github_outputs(plan, arguments.max_layers, arguments.max_shards))
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
