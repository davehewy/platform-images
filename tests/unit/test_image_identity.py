from __future__ import annotations

from difflib import SequenceMatcher as RealSequenceMatcher

import platform_images.image_identity as image_identity
from platform_images.image_identity import (
    build_image_identity_resolver,
    registry_relative_repository,
    repository_name,
)


def test_repository_normalization_separates_registry_path_tag_and_digest() -> None:
    assert repository_name("nexus.example.com:5000/gitlab/base:latest") == (
        "nexus.example.com:5000/gitlab/base"
    )
    assert repository_name("nexus.example.com/gitlab/base@sha256:abc") == (
        "nexus.example.com/gitlab/base"
    )
    assert registry_relative_repository("nexus.example.com:5000/gitlab/base:latest") == (
        "gitlab/base"
    )


def test_resolver_matches_logical_canonical_and_legacy_image_identities() -> None:
    resolver = build_image_identity_resolver(
        {"ubuntu-base-24-04"},
        "platform-images",
        repositories={"ubuntu-base-24-04": "gitlab/ubuntu-base-24-04"},
        aliases={"ubuntu-base-24-04": ("old-nexus.example.com/legacy/ubuntu-base-24-04",)},
    )

    expected = {"ubuntu-base-24-04"}
    assert resolver.candidates("ubuntu-base-24-04") == expected
    assert resolver.candidates("nexus.example.com/gitlab/ubuntu-base-24-04:latest") == expected
    assert (
        resolver.candidates("old-nexus.example.com/legacy/ubuntu-base-24-04@sha256:abc") == expected
    )
    assert resolver.is_managed("nexus.example.com/gitlab/missing:latest")
    assert not resolver.is_managed("docker.io/library/ubuntu:24.04")


def test_resolver_matches_any_qualified_reference_with_an_exact_local_basename() -> None:
    resolver = build_image_identity_resolver(
        {"ubuntu24-04-base"},
        "platform-images",
    )

    assert resolver.candidates("nexus.example.com/gitlab-runner/ubuntu24-04-base:latest") == {
        "ubuntu24-04-base"
    }
    assert resolver.candidates("ghcr.io/team/ubuntu24-04-base@sha256:abc") == {"ubuntu24-04-base"}


def test_explicit_external_repository_suppresses_automatic_basename_matching() -> None:
    resolver = build_image_identity_resolver(
        {"base"},
        "platform-images",
        external_repositories={"docker.io/vendor/base"},
    )

    assert not resolver.candidates("docker.io/vendor/base:latest")
    assert resolver.is_explicit_external("docker.io/vendor/base:latest")
    assert not resolver.probable_local_targets("docker.io/vendor/base:latest")
    assert resolver.candidates("nexus.example.com/team/base:latest") == {"base"}


def test_resolver_reports_only_strong_probable_local_matches() -> None:
    resolver = build_image_identity_resolver(
        {"ubuntu24-04-base", "application"},
        "platform-images",
    )

    assert resolver.probable_local_targets(
        "nexus.example.com/gitlab-runner/ubuntu24-base:latest"
    ) == ("ubuntu24-04-base",)
    assert not resolver.probable_local_targets("docker.io/library/ubuntu:24.04")


def test_resolver_memoizes_repeated_probable_match_scans(monkeypatch) -> None:
    comparisons = 0

    def counting_sequence_matcher(*args, **kwargs):
        nonlocal comparisons
        comparisons += 1
        return RealSequenceMatcher(*args, **kwargs)

    monkeypatch.setattr(image_identity, "SequenceMatcher", counting_sequence_matcher)
    resolver = build_image_identity_resolver(
        {f"service-{index:03d}" for index in range(360)},
        "platform-images",
    )

    expected = resolver.probable_local_targets("nexus.example.com/retired/service042:latest")
    first_scan_comparisons = comparisons

    assert expected[0] == "service-042"
    assert first_scan_comparisons == 360
    assert (
        resolver.probable_local_targets("nexus.example.com/retired/service042@sha256:deadbeef")
        == expected
    )
    assert comparisons == first_scan_comparisons


def test_resolver_exposes_identity_collisions_without_guessing() -> None:
    resolver = build_image_identity_resolver(
        {"base", "legacy"},
        "platform-images",
        repositories={"base": "gitlab/base"},
        aliases={"legacy": ("nexus.example.com/gitlab/base",)},
    )

    assert resolver.collisions["gitlab/base"] == {"base", "legacy"}


def test_resolver_distinguishes_internal_registry_from_owned_repository_prefix() -> None:
    resolver = build_image_identity_resolver(
        {"base"},
        "published/team",
        internal_registries={"nexus.internal:8088"},
        managed_repository_prefixes={
            "nexus.internal:8088/some-repo/sub-repo",
        },
    )

    assert resolver.is_internal_registry("nexus.internal:8088/unrelated/vendor-image:v1")
    assert resolver.is_managed("nexus.internal:8088/some-repo/sub-repo/not-discovered:v1")
    assert not resolver.is_managed("nexus.internal:8088/unrelated/not-discovered:v1")
    assert not resolver.is_internal_registry("docker.io/library/alpine:3.22")
