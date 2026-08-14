from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_images.errors import PlatformImagesError
from platform_images.manifests import (
    build_manifest_data,
    load_build_manifest,
    manifest_promotions,
    write_json_file,
)
from platform_images.models import BuildMode, BuildPlan, BuildPlanTarget

DIGEST = "sha256:" + "a" * 64


def _plan() -> BuildPlan:
    return BuildPlan(
        1,
        BuildMode.DEFAULT_BRANCH,
        "before",
        "commit",
        (
            BuildPlanTarget(
                "base",
                ("changed:containers/base/Containerfile",),
                (),
                (),
                "containers/base/Containerfile",
                "containers/base",
                "registry.example/platform-images/base:sha-commit",
                {},
                True,
            ),
        ),
        (),
    )


def _result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "target": "base",
        "commit_sha": "commit",
        "source": "https://example.com/team/repository",
        "reference": "registry.example/platform-images/base:sha-commit",
        "digest": DIGEST,
        "immutable_reference": f"registry.example/platform-images/base@{DIGEST}",
        "input_references": {},
    }
    result.update(overrides)
    return result


def test_manifest_verifies_plan_and_records_commit_to_digest(tmp_path: Path) -> None:
    result_path = tmp_path / "results" / "base.json"
    write_json_file(result_path, _result())

    manifest = build_manifest_data(
        [result_path.parent],
        plan=_plan(),
        source="https://example.com/team/repository",
    )

    assert manifest == {
        "schema_version": 1,
        "mode": "default_branch",
        "base_sha": "before",
        "commit_sha": "commit",
        "source": "https://example.com/team/repository",
        "images": {
            "base": {
                "reference": "registry.example/platform-images/base:sha-commit",
                "digest": DIGEST,
                "immutable_reference": f"registry.example/platform-images/base@{DIGEST}",
                "input_references": {},
            }
        },
    }


def test_manifest_supports_a_verified_empty_change_set() -> None:
    plan = BuildPlan(1, BuildMode.MERGE_REQUEST, "before", "commit", (), ())

    manifest = build_manifest_data(
        [],
        plan=plan,
        source="https://example.com/team/repository",
    )

    assert manifest["images"] == {}
    assert manifest["commit_sha"] == "commit"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"commit_sha": "other"}, "belongs to commit"),
        ({"reference": "registry.example/platform-images/base:wrong"}, "planned output"),
        (
            {"immutable_reference": "registry.example/platform-images/base@sha256:bad"},
            "does not match",
        ),
    ],
)
def test_manifest_rejects_untrusted_or_mismatched_results(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    path = tmp_path / "base.json"
    write_json_file(path, _result(**overrides))

    with pytest.raises(PlatformImagesError, match=message):
        build_manifest_data(
            [path],
            plan=_plan(),
            source="https://example.com/team/repository",
        )


def test_manifest_rejects_missing_expected_result() -> None:
    with pytest.raises(PlatformImagesError, match="missing build results for targets: base"):
        build_manifest_data(
            [],
            mode=BuildMode.DEFAULT_BRANCH,
            commit_sha="commit",
            source="https://example.com/team/repository",
            expected_targets=["base"],
        )


def test_manifest_round_trip_and_semantic_promotion_are_digest_pinned(tmp_path: Path) -> None:
    manifest_path = tmp_path / "image-build-manifest.json"
    manifest = build_manifest_data(
        [],
        mode=BuildMode.DEFAULT_BRANCH,
        commit_sha="commit",
        source="https://example.com/team/repository",
    )
    manifest["images"] = {
        "base": {
            "reference": "registry.example/platform-images/base:sha-commit",
            "digest": DIGEST,
            "immutable_reference": f"registry.example/platform-images/base@{DIGEST}",
            "input_references": {},
        }
    }
    write_json_file(manifest_path, manifest)

    loaded = load_build_manifest(manifest_path)
    assert manifest_promotions(loaded, "v1.2.3", expected_commit="commit") == (
        {
            "target": "base",
            "source": f"registry.example/platform-images/base@{DIGEST}",
            "destination": "registry.example/platform-images/base:v1.2.3",
        },
    )


def test_manifest_promotion_rejects_wrong_commit_invalid_tag_and_unknown_image(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "default_branch",
                "base_sha": "before",
                "commit_sha": "commit",
                "source": "https://example.com/team/repository",
                "images": {
                    "base": {
                        "reference": "registry.example/platform-images/base:sha-commit",
                        "digest": DIGEST,
                        "immutable_reference": f"registry.example/platform-images/base@{DIGEST}",
                        "input_references": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manifest = load_build_manifest(path)

    with pytest.raises(PlatformImagesError, match="belongs to commit"):
        manifest_promotions(manifest, "v1.2.3", expected_commit="other")
    with pytest.raises(PlatformImagesError, match="valid container tag"):
        manifest_promotions(manifest, "v1.2.3+invalid", expected_commit="commit")
    with pytest.raises(PlatformImagesError, match="not present"):
        manifest_promotions(
            manifest,
            "v1.2.3",
            expected_commit="commit",
            selected_images=["api"],
        )

    manifest["mode"] = "merge_request"
    with pytest.raises(PlatformImagesError, match="default-branch"):
        manifest_promotions(manifest, "v1.2.3", expected_commit="commit")
