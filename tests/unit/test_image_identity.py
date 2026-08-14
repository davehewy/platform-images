from __future__ import annotations

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


def test_resolver_exposes_identity_collisions_without_guessing() -> None:
    resolver = build_image_identity_resolver(
        {"base", "legacy"},
        "platform-images",
        repositories={"base": "gitlab/base"},
        aliases={"legacy": ("nexus.example.com/gitlab/base",)},
    )

    assert resolver.collisions["gitlab/base"] == {"base", "legacy"}
