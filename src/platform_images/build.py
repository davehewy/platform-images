from __future__ import annotations

import json
import os
import re
import shlex
import tarfile
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from platform_images.backends import (
    BUILDERS,
    PushStrategy,
    inspect_command,
    push_command,
    validate_execution_pair,
)
from platform_images.errors import PlatformImagesError, ProcessError
from platform_images.graph import ImageGraph
from platform_images.models import BuildBackend, BuildPlan, BuildPlanTarget, RegistryTransport
from platform_images.process import ProcessRunner
from platform_images.references import immutable_reference

DIGEST_RE = re.compile(r"sha256:[0-9A-Fa-f]{64}")
IMAGE_MANIFEST_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}


def _valid_digest(value: object) -> str | None:
    return value if isinstance(value, str) and DIGEST_RE.fullmatch(value) else None


def build_command(
    target: BuildPlanTarget,
    *,
    labels: Mapping[str, str] | None = None,
    builder: BuildBackend | None = None,
    engine: BuildBackend | None = None,
    push: bool = False,
    metadata_file: Path | None = None,
    context_overrides: Mapping[str, str] | None = None,
) -> list[str]:
    if builder is not None and engine is not None and builder is not engine:
        raise ValueError("builder and legacy engine select different backends")
    builder = builder or engine or BuildBackend.PODMAN
    capabilities = BUILDERS[builder]
    if builder is BuildBackend.DOCKER:
        command = ["docker", "buildx", "build", "--push" if push else "--load"]
        if metadata_file is not None:
            command.extend(["--metadata-file", str(metadata_file)])
    elif builder is BuildBackend.NERDCTL:
        if metadata_file is not None:
            raise ValueError("nerdctl builds do not accept a Buildx metadata file")
        command = ["nerdctl", "build"]
        if push:
            command.extend(["--output", f"type=image,name={target.output_ref},push=true"])
    else:
        if metadata_file is not None:
            raise ValueError(f"{builder.value} builds do not accept a Buildx metadata file")
        if push:
            raise ValueError(f"{builder.value} requires a separate registry push")
        command = [capabilities.executable, "build"]
    build_contexts = target.build_contexts or target.input_refs
    for context_name, reference in sorted(build_contexts.items()):
        context = (context_overrides or {}).get(reference)
        if context is None:
            context = f"{capabilities.context_scheme}{reference}"
        command.extend(["--build-context", f"{context_name}={context}"])
    for name, value in sorted((labels or {}).items()):
        command.extend(["--label", f"{name}={value}"])
    command.extend(["--file", target.dockerfile])
    if not (builder is BuildBackend.NERDCTL and push):
        command.extend(["--tag", target.output_ref])
    command.append(target.context)
    return command


def execute_local_plan(
    plan: BuildPlan,
    *,
    root: Path,
    dry_run: bool = False,
    runner: ProcessRunner | None = None,
    builder: BuildBackend | None = None,
    engine: BuildBackend | None = None,
) -> tuple[str, ...]:
    if builder is not None and engine is not None and builder is not engine:
        raise ValueError("builder and legacy engine select different backends")
    builder = builder or engine or BuildBackend.PODMAN
    process = runner or ProcessRunner()
    rendered: list[str] = []
    required_as_input = {
        reference for target in plan.targets for reference in target.input_refs.values()
    }
    with tempfile.TemporaryDirectory(prefix="platform-images-local-contexts-") as directory:
        context_directory = Path(directory)
        local_contexts: dict[str, str] = {}
        for target in plan.targets:
            overrides = {
                reference: local_contexts[reference]
                for reference in (target.build_contexts or target.input_refs).values()
                if reference in local_contexts
            }
            command = build_command(
                target,
                builder=builder,
                context_overrides=overrides,
            )
            rendered.append(shlex.join(command))
            if not dry_run:
                process.run(command, cwd=root)
            if builder is not BuildBackend.NERDCTL or target.output_ref not in required_as_input:
                continue
            archive = context_directory / f"{target.name}.tar"
            export_command = [
                "nerdctl",
                "save",
                "--output",
                str(archive),
                target.output_ref,
            ]
            rendered.append(shlex.join(export_command))
            if dry_run:
                local_contexts[target.output_ref] = (
                    f"oci-layout://<verified-temporary-layout-for-{target.name}>"
                )
                continue
            process.run(export_command, cwd=root)
            layout = context_directory / target.name
            layout.mkdir()
            try:
                with tarfile.open(archive) as image_archive:
                    image_archive.extractall(layout, filter="data")
            except (OSError, tarfile.TarError) as exc:
                raise PlatformImagesError(
                    f"nerdctl did not export a valid OCI layout for {target.name}: {exc}"
                ) from exc
            local_contexts[target.output_ref] = _oci_layout_reference(layout, target.output_ref)
    return tuple(rendered)


def _oci_layout_reference(layout: Path, image_reference: str) -> str:
    try:
        index = json.loads((layout / "index.json").read_text(encoding="utf-8"))
        manifests = index["manifests"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PlatformImagesError("nerdctl export does not contain a valid OCI index") from exc
    if not isinstance(manifests, list):
        raise PlatformImagesError("nerdctl OCI index manifests must be an array")
    candidates = [
        item
        for item in manifests
        if isinstance(item, dict) and item.get("mediaType") in IMAGE_MANIFEST_MEDIA_TYPES
    ]
    if len(candidates) != 1:
        raise PlatformImagesError(
            f"nerdctl OCI export for {image_reference!r} must contain exactly one image manifest"
        )
    digest = candidates[0].get("digest")
    if _valid_digest(digest) is None:
        raise PlatformImagesError(
            f"nerdctl OCI export does not identify {image_reference!r} by sha256 digest"
        )
    # nerdctl's build-context parser accepts a layout path (not a digest-qualified URI), reads its
    # index, and selects the image manifest. Requiring a single validated manifest above makes that
    # independently selected descriptor unambiguous.
    return f"oci-layout://{layout.resolve().as_posix()}"


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


def _result(
    target_name: str,
    output_ref: str,
    digest: str,
    *,
    commit_sha: str,
    source: str,
    input_refs: Mapping[str, str],
) -> dict[str, object]:
    if _valid_digest(digest) is None:
        raise PlatformImagesError(f"container tooling returned an invalid image digest: {digest!r}")
    return {
        "schema_version": 1,
        "target": target_name,
        "commit_sha": commit_sha,
        "source": source,
        "reference": output_ref,
        "digest": digest,
        "immutable_reference": immutable_reference(output_ref, digest),
        "input_references": dict(sorted(input_refs.items())),
    }


def _inspect_digest(image: Mapping[str, object]) -> str:
    for key in ("Digest", "FromImageDigest"):
        digest = _valid_digest(image.get(key))
        if digest is not None:
            return digest
    for key in ("RepoDigests", "repoDigests"):
        repo_digests = image.get(key)
        if isinstance(repo_digests, list):
            for reference in repo_digests:
                if isinstance(reference, str) and "@sha256:" in reference:
                    digest = _valid_digest("sha256:" + reference.split("@sha256:", 1)[1])
                    if digest is not None:
                        return digest
    raise PlatformImagesError("existing image does not expose a registry digest")


def _inspect_labels(image: Mapping[str, object]) -> Mapping[str, object] | None:
    labels = image.get("Labels")
    if isinstance(labels, dict):
        return labels
    for key in ("Config", "config"):
        config = image.get(key)
        if isinstance(config, dict) and isinstance(config.get("Labels"), dict):
            return config["Labels"]  # type: ignore[return-value]
    # Buildah exposes both Docker and OCI configuration views.
    for key in ("Docker", "OCIv1"):
        view = image.get(key)
        if not isinstance(view, dict):
            continue
        config = view.get("config")
        if isinstance(config, dict) and isinstance(config.get("Labels"), dict):
            return config["Labels"]  # type: ignore[return-value]
    return None


def _inspection_image(parsed: object, transport: RegistryTransport) -> Mapping[str, object]:
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        return parsed[0]
    if transport is RegistryTransport.BUILDAH and isinstance(parsed, dict):
        return parsed
    raise PlatformImagesError(
        f"{transport.value} returned an unexpected inspection result for an existing image"
    )


def _existing_ci_result(
    process: ProcessRunner,
    *,
    target_name: str,
    output_ref: str,
    identity_labels: Mapping[str, str],
    commit_sha: str,
    source: str,
    input_refs: Mapping[str, str],
    root: Path,
    transport: RegistryTransport,
    required: bool = False,
) -> dict[str, object] | None:
    executable = transport.value
    try:
        process.run([executable, "pull", output_ref], cwd=root, capture_output=True)
    except ProcessError as exc:
        if required:
            raise PlatformImagesError(
                f"built image could not be pulled for digest verification: {output_ref}: {exc}"
            ) from exc
        return None
    try:
        result = process.run(inspect_command(transport, output_ref), cwd=root, capture_output=True)
        parsed = json.loads(result.stdout)
        image = _inspection_image(parsed, transport)
    except (ProcessError, json.JSONDecodeError) as exc:
        raise PlatformImagesError(
            f"output image already exists but its identity cannot be inspected: {output_ref}: {exc}"
        ) from exc
    labels = _inspect_labels(image)
    mismatches = [
        name
        for name, value in sorted(identity_labels.items())
        if labels is None or labels.get(name) != value
    ]
    if mismatches:
        raise PlatformImagesError(
            f"immutable output tag collision for {output_ref}; identity label mismatch: "
            + ", ".join(mismatches)
        )
    return _result(
        target_name,
        output_ref,
        _inspect_digest(image),
        commit_sha=commit_sha,
        source=source,
        input_refs=input_refs,
    )


def execute_ci_build(
    graph: ImageGraph,
    target_name: str,
    output_ref: str,
    input_refs: Mapping[str, str],
    *,
    root: Path,
    environment: Mapping[str, str] | None = None,
    runner: ProcessRunner | None = None,
    builder: BuildBackend | None = None,
    registry_transport: RegistryTransport | None = None,
    engine: BuildBackend | None = None,
) -> dict[str, object]:
    if builder is not None and engine is not None and builder is not engine:
        raise ValueError("builder and legacy engine select different backends")
    explicitly_selected = builder or engine
    builder = builder or engine or BuildBackend.PODMAN
    if registry_transport is None:
        registry_transport = (
            RegistryTransport(explicitly_selected.value)
            if explicitly_selected is not None
            else RegistryTransport.PODMAN
        )
    validate_execution_pair(builder, registry_transport)
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
        graph.build_contexts(target_name, input_refs),
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
        commit_sha=revision,
        source=source,
        input_refs=input_refs,
        root=root,
        transport=registry_transport,
    ):
        return existing
    labels = {
        **identity_labels,
        "org.opencontainers.image.created": _created_label(env),
    }
    with tempfile.TemporaryDirectory(prefix="platform-images-digest-") as directory:
        digest_file = Path(directory) / "digest"
        strategy = BUILDERS[builder].push_strategy
        if strategy is PushStrategy.BUILDX_METADATA:
            metadata_file = Path(directory) / "metadata.json"
            process.run(
                build_command(
                    plan_target,
                    labels=labels,
                    builder=builder,
                    push=True,
                    metadata_file=metadata_file,
                ),
                cwd=root,
            )
            try:
                metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
                digest = metadata["containerimage.digest"]
            except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise PlatformImagesError(
                    "docker buildx did not write a valid containerimage.digest"
                ) from exc
        elif strategy is PushStrategy.DIRECT_INSPECT:
            process.run(
                build_command(plan_target, labels=labels, builder=builder, push=True),
                cwd=root,
            )
            verified = _existing_ci_result(
                process,
                target_name=target_name,
                output_ref=output_ref,
                identity_labels=identity_labels,
                commit_sha=revision,
                source=source,
                input_refs=input_refs,
                root=root,
                transport=registry_transport,
                required=True,
            )
            if verified is None:  # pragma: no cover - required=True makes this unreachable
                raise AssertionError("required registry verification returned no result")
            return verified
        else:
            process.run(build_command(plan_target, labels=labels, builder=builder), cwd=root)
            process.run(
                push_command(
                    registry_transport,
                    output_ref,
                    digest_file=str(digest_file),
                ),
                cwd=root,
            )
            try:
                digest = digest_file.read_text(encoding="utf-8").strip()
            except FileNotFoundError as exc:
                raise PlatformImagesError(
                    f"{registry_transport.value} push did not create the requested digest file"
                ) from exc
    if _valid_digest(digest) is None:
        raise PlatformImagesError(f"container engine returned an invalid image digest: {digest!r}")
    return _result(
        target_name,
        output_ref,
        digest,
        commit_sha=revision,
        source=source,
        input_refs=input_refs,
    )


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


def result_json(result: Mapping[str, object]) -> str:
    return json.dumps(dict(result), sort_keys=True)
