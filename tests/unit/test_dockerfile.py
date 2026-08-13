from __future__ import annotations

import pytest

from platform_images.dockerfile import parse_dockerfile
from platform_images.models import ReferenceKind

TARGETS = frozenset({"base", "tooling", "curl"})


def parse(text: str):
    return parse_dockerfile(
        text,
        target_names=TARGETS,
        internal_namespace="platform-images",
        allowed_short_external_images=frozenset({"alpine", "busybox"}),
    )


@pytest.mark.parametrize("instruction", ["FROM base", "from base", "FrOm base"])
def test_from_is_case_insensitive_and_local(instruction: str) -> None:
    reference = parse(instruction).references[0]
    assert reference.kind is ReferenceKind.LOCAL_TARGET
    assert reference.resolved == "base"


@pytest.mark.parametrize(
    "reference",
    [
        "registry.example.com/platform-images/base:sha-123",
        "registry.example.com/platform-images/base@sha256:abcd",
        "platform-images/base:main",
    ],
)
def test_configured_namespace_resolves_local(reference: str) -> None:
    parsed = parse(f"FROM {reference}\n")
    assert parsed.references[0].kind is ReferenceKind.LOCAL_TARGET
    assert parsed.references[0].resolved == "base"


def test_arbitrary_basename_match_is_external() -> None:
    reference = parse("FROM unrelated.example/team/base:latest\n").references[0]
    assert reference.kind is ReferenceKind.EXTERNAL_IMAGE


def test_platform_multistage_and_alias_order() -> None:
    parsed = parse(
        """\
FROM --platform=linux/amd64 alpine:3.22 AS builder
RUN echo x
FROM builder AS final
COPY --from=builder /out /out
"""
    )
    assert [reference.kind for reference in parsed.references] == [
        ReferenceKind.EXTERNAL_IMAGE,
        ReferenceKind.STAGE_ALIAS,
        ReferenceKind.STAGE_ALIAS,
    ]
    assert parsed.stage_aliases == (("builder", 1), ("final", 3))


def test_stage_alias_takes_precedence_over_colliding_target() -> None:
    parsed = parse("FROM alpine AS base\nFROM base\n")
    assert parsed.references[1].kind is ReferenceKind.STAGE_ALIAS


def test_global_arg_defaults_resolve() -> None:
    parsed = parse(
        """\
ARG REGISTRY=registry.example.com
ARG VERSION=123
FROM ${REGISTRY}/platform-images/base:${VERSION}
"""
    )
    assert parsed.references[0].kind is ReferenceKind.LOCAL_TARGET
    assert parsed.references[0].resolved == "base"


@pytest.mark.parametrize(
    "text",
    [
        "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n",
        "ARG REGISTRY=registry.example.com\nFROM ${REGISTRY}/${MISSING}/base\n",
    ],
)
def test_unresolved_args_are_not_external(text: str) -> None:
    assert parse(text).references[0].kind is ReferenceKind.UNRESOLVED


def test_copy_from_local_external_and_stage() -> None:
    parsed = parse(
        """\
FROM alpine AS builder
COPY --from=builder /one /one
COPY --from=tooling /two /two
COPY --from=busybox:1.37 /three /three
"""
    )
    assert [reference.kind for reference in parsed.references] == [
        ReferenceKind.EXTERNAL_IMAGE,
        ReferenceKind.STAGE_ALIAS,
        ReferenceKind.LOCAL_TARGET,
        ReferenceKind.EXTERNAL_IMAGE,
    ]


def test_copy_from_numeric_stage_index_is_not_an_image_dependency() -> None:
    parsed = parse("FROM alpine AS builder\nRUN echo x\nCOPY --from=0 /out /out\n")
    assert parsed.references[1].kind is ReferenceKind.STAGE_ALIAS
    assert parsed.references[1].stage_alias == "0"


def test_add_and_run_mount_create_local_dependency_edges() -> None:
    parsed = parse(
        "FROM alpine\n"
        "ADD --from=tooling /bin/tool /bin/tool\n"
        "RUN --mount=type=bind,from=base,source=/,target=/mnt echo mounted\n"
    )
    assert [reference.resolved for reference in parsed.references] == [
        "alpine",
        "tooling",
        "base",
    ]
    assert [reference.kind for reference in parsed.references[1:]] == [
        ReferenceKind.LOCAL_TARGET,
        ReferenceKind.LOCAL_TARGET,
    ]


def test_multiple_run_mounts_are_all_parsed() -> None:
    parsed = parse(
        "FROM alpine\nRUN --mount=from=base,target=/base --mount=from=tooling,target=/tools true\n"
    )
    assert [reference.resolved for reference in parsed.references[1:]] == ["base", "tooling"]


def test_dockerfile_heredoc_content_is_not_parsed_as_instructions() -> None:
    parsed = parse(
        "FROM alpine\n"
        "RUN <<'SCRIPT'\n"
        "FROM bsae\n"
        "COPY --from=tooling /out /out\n"
        "SCRIPT\n"
        "RUN echo done\n"
    )
    assert len(parsed.references) == 1
    assert parsed.references[0].resolved == "alpine"
    assert parsed.syntax_errors == ()


def test_unterminated_heredoc_and_continuation_are_reported() -> None:
    heredoc = parse("FROM alpine\nRUN <<EOF\necho unfinished\n")
    continuation = parse("FROM alpine \\\n")
    assert heredoc.syntax_errors == ((2, "unterminated heredoc 'EOF'"),)
    assert continuation.syntax_errors == ((1, "unterminated line continuation"),)


def test_line_continuations_and_comments_preserve_first_line() -> None:
    parsed = parse(
        """\
# syntax=docker/dockerfile:1
FROM --platform=linux/amd64 \\
  base AS build # valid trailing comment
COPY --from=build \\
  /out /out
"""
    )
    assert parsed.references[0].line_number == 2
    assert parsed.references[0].resolved == "base"
    assert parsed.references[1].kind is ReferenceKind.STAGE_ALIAS


def test_internal_typo_is_unresolved() -> None:
    reference = parse("FROM registry.example.com/platform-images/bsae:main\n").references[0]
    assert reference.kind is ReferenceKind.UNRESOLVED
    assert reference.resolved == "registry.example.com/platform-images/bsae:main"


def test_unqualified_typo_is_unresolved_instead_of_becoming_external() -> None:
    reference = parse("FROM bsae\n").references[0]
    assert reference.kind is ReferenceKind.UNRESOLVED
    assert reference.resolved == "bsae"


def test_empty_copy_from_is_unresolved() -> None:
    reference = parse("FROM alpine\nCOPY --from= /out /out\n").references[1]
    assert reference.kind is ReferenceKind.UNRESOLVED
