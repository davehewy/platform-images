from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from platform_images.errors import PlatformImagesError
from platform_images.models import BuildMode, BuildPlan
from platform_images.references import immutable_reference

DIGEST_RE = re.compile(r"sha256:[0-9A-Fa-f]{64}")
TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


def write_json_file(path: Path, data: Mapping[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        raise PlatformImagesError(f"unable to write JSON artifact {path}: {exc}") from exc


def result_files(paths: Iterable[Path]) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(item for item in path.rglob("*.json") if item.is_file())
        elif path.is_file():
            files.append(path)
        else:
            raise PlatformImagesError(f"build result path does not exist: {path}")
    return tuple(sorted(set(files), key=lambda item: item.as_posix()))


def _read_json_object(path: Path, kind: str) -> dict[str, object]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformImagesError(f"unable to read {kind} {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PlatformImagesError(f"{kind} {path} must contain a JSON object")
    return parsed


def _string(data: Mapping[str, object], key: str, description: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise PlatformImagesError(f"{description} has an invalid {key!r} field")
    return value


def _input_references(data: Mapping[str, object], description: str) -> dict[str, str]:
    value = data.get("input_references")
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and name and isinstance(reference, str) and reference
        for name, reference in value.items()
    ):
        raise PlatformImagesError(f"{description} has an invalid 'input_references' field")
    return dict(sorted(value.items()))


def _validated_result(data: Mapping[str, object], description: str) -> dict[str, object]:
    if data.get("schema_version") != 1:
        raise PlatformImagesError(f"{description} uses an unsupported result schema")
    target = _string(data, "target", description)
    commit_sha = _string(data, "commit_sha", description)
    source = _string(data, "source", description)
    reference = _string(data, "reference", description)
    digest = _string(data, "digest", description)
    if DIGEST_RE.fullmatch(digest) is None:
        raise PlatformImagesError(f"{description} has an invalid image digest")
    pinned = _string(data, "immutable_reference", description)
    if pinned != immutable_reference(reference, digest):
        raise PlatformImagesError(
            f"{description} immutable_reference does not match its reference and digest"
        )
    inputs = _input_references(data, description)
    return {
        "target": target,
        "commit_sha": commit_sha,
        "source": source,
        "reference": reference,
        "digest": digest,
        "immutable_reference": pinned,
        "input_references": inputs,
    }


def build_manifest_data(
    files: Iterable[Path],
    *,
    mode: BuildMode | None = None,
    commit_sha: str | None = None,
    source: str | None = None,
    base_sha: str | None = None,
    expected_targets: Iterable[str] = (),
    plan: BuildPlan | None = None,
) -> dict[str, object]:
    if plan is not None:
        if plan.mode is BuildMode.LOCAL:
            raise PlatformImagesError("a build manifest requires a CI plan")
        mode = plan.mode
        commit_sha = plan.head_sha
        base_sha = plan.base_sha
        expected_targets = (target.name for target in plan.targets)
    if mode is None or mode is BuildMode.LOCAL:
        raise PlatformImagesError("build manifest mode must be merge_request or default_branch")
    if not commit_sha:
        raise PlatformImagesError("CI_COMMIT_SHA or --commit-sha is required for a build manifest")
    if not source:
        raise PlatformImagesError("CI_PROJECT_URL or --source is required for a build manifest")

    expected = set(expected_targets)
    images: dict[str, dict[str, object]] = {}
    plan_targets = {target.name: target for target in plan.targets} if plan is not None else {}
    for path in result_files(files):
        result = _validated_result(_read_json_object(path, "build result"), f"build result {path}")
        target = str(result["target"])
        if target in images:
            raise PlatformImagesError(f"duplicate build result for target: {target}")
        if result["commit_sha"] != commit_sha:
            raise PlatformImagesError(
                f"build result for {target} belongs to commit {result['commit_sha']}, "
                f"not {commit_sha}"
            )
        if result["source"] != source:
            raise PlatformImagesError(
                f"build result for {target} belongs to source {result['source']!r}, not {source!r}"
            )
        if plan_target := plan_targets.get(target):
            if result["reference"] != plan_target.output_ref:
                raise PlatformImagesError(
                    f"build result for {target} does not match its planned output reference"
                )
            if result["input_references"] != dict(sorted(plan_target.input_refs.items())):
                raise PlatformImagesError(
                    f"build result for {target} does not match its planned dependency inputs"
                )
        images[target] = {
            "reference": result["reference"],
            "digest": result["digest"],
            "immutable_reference": result["immutable_reference"],
            "input_references": result["input_references"],
        }

    actual = set(images)
    if missing := sorted(expected - actual):
        raise PlatformImagesError("missing build results for targets: " + ", ".join(missing))
    if unexpected := sorted(actual - expected):
        raise PlatformImagesError("unexpected build results for targets: " + ", ".join(unexpected))
    return {
        "schema_version": 1,
        "mode": mode.value,
        "base_sha": base_sha,
        "commit_sha": commit_sha,
        "source": source,
        "images": {name: images[name] for name in sorted(images)},
    }


def load_build_manifest(path: Path) -> dict[str, object]:
    data = _read_json_object(path, "build manifest")
    if data.get("schema_version") != 1:
        raise PlatformImagesError("build manifest uses an unsupported schema")
    try:
        mode = BuildMode(_string(data, "mode", "build manifest"))
    except ValueError as exc:
        raise PlatformImagesError("build manifest has an invalid mode") from exc
    if mode is BuildMode.LOCAL:
        raise PlatformImagesError("build manifest mode must be merge_request or default_branch")
    commit_sha = _string(data, "commit_sha", "build manifest")
    source = _string(data, "source", "build manifest")
    images = data.get("images")
    if not isinstance(images, dict):
        raise PlatformImagesError("build manifest has an invalid 'images' field")
    validated: dict[str, dict[str, object]] = {}
    for target, image in images.items():
        if not isinstance(target, str) or not target or not isinstance(image, dict):
            raise PlatformImagesError("build manifest has an invalid image entry")
        result = _validated_result(
            {
                "schema_version": 1,
                "target": target,
                "commit_sha": commit_sha,
                "source": source,
                **image,
            },
            f"build manifest image {target!r}",
        )
        validated[target] = {
            "reference": result["reference"],
            "digest": result["digest"],
            "immutable_reference": result["immutable_reference"],
            "input_references": result["input_references"],
        }
    return {
        "schema_version": 1,
        "mode": data["mode"],
        "base_sha": data.get("base_sha"),
        "commit_sha": commit_sha,
        "source": source,
        "images": {name: validated[name] for name in sorted(validated)},
    }


def manifest_promotions(
    manifest: Mapping[str, object],
    tag: str,
    *,
    expected_commit: str,
    selected_images: Sequence[str] = (),
) -> tuple[dict[str, str], ...]:
    if TAG_RE.fullmatch(tag) is None:
        raise PlatformImagesError(
            "release tag must be a valid container tag of at most 128 characters"
        )
    commit_sha = _string(manifest, "commit_sha", "build manifest")
    if manifest.get("mode") != BuildMode.DEFAULT_BRANCH.value:
        raise PlatformImagesError("only a default-branch build manifest can be released")
    if commit_sha != expected_commit:
        raise PlatformImagesError(
            f"build manifest belongs to commit {commit_sha}, not {expected_commit}"
        )
    images = manifest.get("images")
    if not isinstance(images, dict):
        raise PlatformImagesError("build manifest has an invalid 'images' field")
    selected = set(selected_images) if selected_images else set(images)
    if unknown := sorted(selected - set(images)):
        raise PlatformImagesError(
            "images are not present in the build manifest: " + ", ".join(unknown)
        )
    if not selected:
        raise PlatformImagesError("build manifest contains no images to promote")

    promotions: list[dict[str, str]] = []
    for target in sorted(selected):
        image = images[target]
        if not isinstance(image, dict):  # load_build_manifest normally catches this first
            raise PlatformImagesError(f"build manifest has an invalid image entry: {target}")
        source = _string(image, "immutable_reference", f"build manifest image {target!r}")
        repository, separator, _digest = source.partition("@")
        if not separator:
            raise PlatformImagesError(f"build manifest image {target!r} is not digest-pinned")
        promotions.append(
            {"target": target, "source": source, "destination": f"{repository}:{tag}"}
        )
    return tuple(promotions)
