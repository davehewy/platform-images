from __future__ import annotations

import json
import os
import shlex
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from platform_images.errors import PlatformImagesError, ProcessError
from platform_images.graph import ImageGraph
from platform_images.models import BuildPlan, BuildPlanTarget
from platform_images.process import ProcessRunner
from platform_images.references import immutable_reference


def build_command(
    target: BuildPlanTarget,
    *,
    labels: Mapping[str, str] | None = None,
) -> list[str]:
    command = ["podman", "build"]
    for dependency, reference in sorted(target.input_refs.items()):
        command.extend(["--build-context", f"{dependency}=container-image://{reference}"])
    for name, value in sorted((labels or {}).items()):
        command.extend(["--label", f"{name}={value}"])
    command.extend(["--file", target.dockerfile, "--tag", target.output_ref, target.context])
    return command


def execute_local_plan(
    plan: BuildPlan,
    *,
    root: Path,
    dry_run: bool = False,
    runner: ProcessRunner | None = None,
) -> tuple[str, ...]:
    process = runner or ProcessRunner()
    rendered: list[str] = []
    for target in plan.targets:
        command = build_command(target)
        display = shlex.join(command)
        rendered.append(display)
        if not dry_run:
            process.run(command, cwd=root)
    return tuple(rendered)


def _created_label(environment: Mapping[str, str]) -> str:
    epoch = environment.get("SOURCE_DATE_EPOCH")
    if epoch:
        moment = datetime.fromtimestamp(int(epoch), tz=UTC)
    elif timestamp := environment.get("CI_COMMIT_TIMESTAMP"):
        try:
            moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PlatformImagesError(f"invalid CI_COMMIT_TIMESTAMP: {timestamp!r}") from exc
        if moment.tzinfo is None:
            raise PlatformImagesError("CI_COMMIT_TIMESTAMP must include a timezone")
        moment = moment.astimezone(UTC)
    else:
        moment = datetime.now(tz=UTC)
    return moment.isoformat().replace("+00:00", "Z")


def _result(target_name: str, output_ref: str, digest: str) -> dict[str, str]:
    if not digest.startswith("sha256:"):
        raise PlatformImagesError(f"podman returned an invalid image digest: {digest!r}")
    return {
        "target": target_name,
        "reference": output_ref,
        "digest": digest,
        "immutable_reference": immutable_reference(output_ref, digest),
    }


def _inspect_digest(image: Mapping[str, object]) -> str:
    digest = image.get("Digest")
    if isinstance(digest, str) and digest.startswith("sha256:"):
        return digest
    repo_digests = image.get("RepoDigests")
    if isinstance(repo_digests, list):
        for reference in repo_digests:
            if isinstance(reference, str) and "@sha256:" in reference:
                return "sha256:" + reference.split("@sha256:", 1)[1]
    raise PlatformImagesError("existing image does not expose a registry digest")


def _existing_ci_result(
    process: ProcessRunner,
    *,
    target_name: str,
    output_ref: str,
    identity_labels: Mapping[str, str],
    root: Path,
) -> dict[str, str] | None:
    try:
        process.run(["podman", "pull", output_ref], cwd=root, capture_output=True)
    except ProcessError:
        return None
    try:
        result = process.run(
            ["podman", "image", "inspect", output_ref], cwd=root, capture_output=True
        )
        parsed = json.loads(result.stdout)
    except (ProcessError, json.JSONDecodeError) as exc:
        raise PlatformImagesError(
            f"output image already exists but its identity cannot be inspected: {output_ref}: {exc}"
        ) from exc
    if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
        raise PlatformImagesError(
            f"podman returned an unexpected inspection result for existing image: {output_ref}"
        )
    image = parsed[0]
    labels = image.get("Labels")
    if not isinstance(labels, dict):
        config = image.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
    mismatches = [
        name
        for name, value in sorted(identity_labels.items())
        if not isinstance(labels, dict) or labels.get(name) != value
    ]
    if mismatches:
        raise PlatformImagesError(
            f"immutable output tag collision for {output_ref}; identity label mismatch: "
            + ", ".join(mismatches)
        )
    return _result(target_name, output_ref, _inspect_digest(image))


def execute_ci_build(
    graph: ImageGraph,
    target_name: str,
    output_ref: str,
    input_refs: Mapping[str, str],
    *,
    root: Path,
    environment: Mapping[str, str] | None = None,
    runner: ProcessRunner | None = None,
) -> dict[str, str]:
    if target_name not in graph.targets:
        raise PlatformImagesError(f"unknown image target: {target_name}")
    expected = set(graph.dependencies[target_name])
    supplied = set(input_refs)
    if missing := sorted(expected - supplied):
        raise PlatformImagesError("missing required dependency inputs: " + ", ".join(missing))
    if extra := sorted(supplied - expected):
        raise PlatformImagesError("unexpected dependency inputs: " + ", ".join(extra))
    env = dict(os.environ if environment is None else environment)
    revision = env.get("CI_COMMIT_SHA")
    source = env.get("CI_PROJECT_URL")
    if not revision:
        raise PlatformImagesError("CI_COMMIT_SHA is required for a CI image build")
    if not source:
        raise PlatformImagesError("CI_PROJECT_URL is required for a CI image build")
    target = graph.targets[target_name]
    relative_dockerfile = target.dockerfile.relative_to(root).as_posix()
    relative_context = target.context.relative_to(root).as_posix()
    plan_target = BuildPlanTarget(
        target_name,
        ("ci-build",),
        tuple(sorted(expected)),
        (),
        relative_dockerfile,
        relative_context,
        output_ref,
        dict(sorted(input_refs.items())),
        True,
    )
    identity_labels = {
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.source": source,
        "io.platform-images.target": target_name,
        "io.platform-images.input-references": json.dumps(
            dict(sorted(input_refs.items())), separators=(",", ":")
        ),
    }
    process = runner or ProcessRunner()
    if existing := _existing_ci_result(
        process,
        target_name=target_name,
        output_ref=output_ref,
        identity_labels=identity_labels,
        root=root,
    ):
        return existing
    labels = {
        **identity_labels,
        "org.opencontainers.image.created": _created_label(env),
    }
    process.run(build_command(plan_target, labels=labels), cwd=root)
    with tempfile.TemporaryDirectory(prefix="platform-images-digest-") as directory:
        digest_file = Path(directory) / "digest"
        process.run(["podman", "push", "--digestfile", str(digest_file), output_ref], cwd=root)
        try:
            digest = digest_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise PlatformImagesError(
                "podman push did not create the requested digest file"
            ) from exc
    return _result(target_name, output_ref, digest)


def parse_input_references(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, reference = value.partition("=")
        if not separator or not name or not reference:
            raise PlatformImagesError(
                f"invalid --input-ref {value!r}; expected <dependency>=<reference>"
            )
        if name in result:
            raise PlatformImagesError(f"duplicate --input-ref for dependency: {name}")
        result[name] = reference
    return result


def result_json(result: Mapping[str, str]) -> str:
    return json.dumps(dict(result), sort_keys=True)
