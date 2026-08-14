from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

from platform_images.image_identity import (
    ImageIdentityResolver,
    build_image_identity_resolver,
    repository_name,
)
from platform_images.models import ImageReference, ReferenceKind

VARIABLE_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
HEREDOC_RE = re.compile(
    r"<<(?P<tabs>-?)(?:'(?P<single>[^']+)'|\"(?P<double>[^\"]+)\"|(?P<bare>[A-Za-z0-9_.-]+))"
)


@dataclass(frozen=True)
class DockerfileParseResult:
    references: tuple[ImageReference, ...]
    stage_aliases: tuple[tuple[str, int], ...]
    syntax_errors: tuple[tuple[int, str], ...]


def _logical_instructions(
    text: str,
) -> tuple[tuple[tuple[int, str], ...], tuple[tuple[int, str], ...]]:
    instructions: list[tuple[int, str]] = []
    errors: list[tuple[int, str]] = []
    parts: list[str] = []
    first_line = 0
    heredocs: list[tuple[str, bool, int]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if heredocs:
            marker, strip_tabs, _start_line = heredocs[0]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == marker:
                heredocs.pop(0)
            continue
        stripped = line.strip()
        if not parts and (not stripped or stripped.startswith("#")):
            continue
        if not parts:
            first_line = line_number
        continued = line.rstrip().endswith("\\")
        parts.append(line.rstrip()[:-1] if continued else line)
        if not continued:
            logical = " ".join(part.strip() for part in parts).strip()
            if logical:
                instructions.append((first_line, logical))
                heredocs.extend(
                    (
                        match.group("single") or match.group("double") or match.group("bare"),
                        match.group("tabs") == "-",
                        first_line,
                    )
                    for match in HEREDOC_RE.finditer(logical)
                )
            parts = []
    if parts:
        instructions.append((first_line, " ".join(part.strip() for part in parts)))
        errors.append((first_line, "unterminated line continuation"))
    errors.extend(
        (line, f"unterminated heredoc {marker!r}") for marker, _strip_tabs, line in heredocs
    )
    return tuple(instructions), tuple(errors)


def _resolve_variables(raw: str, arguments: dict[str, str]) -> str | None:
    unresolved = False

    def replace(match: re.Match[str]) -> str:
        nonlocal unresolved
        name = match.group(1) or match.group(2)
        if name not in arguments:
            unresolved = True
            return match.group(0)
        return arguments[name]

    resolved = VARIABLE_RE.sub(replace, raw)
    return None if unresolved or "$" in resolved else resolved


def _classify(
    raw: str,
    resolved: str | None,
    instruction: str,
    line_number: int,
    image_identities: ImageIdentityResolver,
    stage_aliases: frozenset[str],
    allowed_short_external_images: frozenset[str],
) -> ImageReference:
    if not resolved:
        return ImageReference(raw, None, instruction, line_number, ReferenceKind.UNRESOLVED)
    if resolved in stage_aliases:
        return ImageReference(
            raw,
            resolved,
            instruction,
            line_number,
            ReferenceKind.STAGE_ALIAS,
            stage_alias=resolved,
        )
    repository = repository_name(resolved)
    candidates = image_identities.candidates(repository)
    if len(candidates) == 1:
        return ImageReference(
            raw,
            next(iter(candidates)),
            instruction,
            line_number,
            ReferenceKind.LOCAL_TARGET,
            source=resolved,
        )
    if image_identities.is_explicit_external(repository):
        return ImageReference(raw, resolved, instruction, line_number, ReferenceKind.EXTERNAL_IMAGE)
    if candidates or image_identities.is_managed(repository):
        return ImageReference(raw, resolved, instruction, line_number, ReferenceKind.UNRESOLVED)
    if "/" not in repository and repository not in allowed_short_external_images:
        return ImageReference(
            raw,
            resolved,
            instruction,
            line_number,
            ReferenceKind.UNRESOLVED,
        )
    return ImageReference(raw, resolved, instruction, line_number, ReferenceKind.EXTERNAL_IMAGE)


def parse_dockerfile(
    text: str,
    *,
    target_names: frozenset[str],
    internal_namespace: str,
    allowed_short_external_images: frozenset[str] = frozenset({"scratch"}),
    image_identities: ImageIdentityResolver | None = None,
    build_arguments: Mapping[str, str] | None = None,
) -> DockerfileParseResult:
    identities = image_identities or build_image_identity_resolver(
        target_names,
        internal_namespace,
    )
    arguments = dict(build_arguments or {})
    aliases: list[tuple[str, int]] = []
    known_aliases: set[str] = set()
    known_stage_indexes: set[str] = set()
    references: list[ImageReference] = []
    saw_from = False
    instructions, syntax_errors = _logical_instructions(text)

    for line_number, logical in instructions:
        try:
            tokens = shlex.split(logical, comments=True, posix=True)
        except ValueError as exc:
            syntax_errors += ((line_number, f"cannot parse instruction: {exc}"),)
            tokens = logical.split()
        if not tokens:
            continue
        instruction = tokens[0].upper()
        if instruction == "ARG" and not saw_from and len(tokens) >= 2:
            name, separator, value = tokens[1].partition("=")
            if separator and name not in arguments:
                arguments[name] = value
            continue
        if instruction == "FROM":
            saw_from = True
            index = 1
            while index < len(tokens) and tokens[index].startswith("--"):
                index += 1
            if index >= len(tokens):
                references.append(
                    ImageReference("", None, "FROM", line_number, ReferenceKind.UNRESOLVED)
                )
                continue
            raw = tokens[index]
            resolved = _resolve_variables(raw, arguments)
            references.append(
                _classify(
                    raw,
                    resolved,
                    "FROM",
                    line_number,
                    identities,
                    frozenset(known_aliases | known_stage_indexes),
                    allowed_short_external_images,
                )
            )
            known_stage_indexes.add(str(len(known_stage_indexes)))
            if index + 2 < len(tokens) and tokens[index + 1].upper() == "AS":
                alias = tokens[index + 2]
                known_aliases.add(alias)
                aliases.append((alias, line_number))
            continue
        if instruction in {"COPY", "ADD"}:
            raw_from: str | None = None
            for index, token in enumerate(tokens[1:], 1):
                if token.startswith("--from="):
                    raw_from = token.partition("=")[2]
                    break
                if token == "--from" and index + 1 < len(tokens):
                    raw_from = tokens[index + 1]
                    break
            if raw_from is not None:
                resolved = _resolve_variables(raw_from, arguments)
                references.append(
                    _classify(
                        raw_from,
                        resolved,
                        f"{instruction} --from",
                        line_number,
                        identities,
                        frozenset(known_aliases | known_stage_indexes),
                        allowed_short_external_images,
                    )
                )
            continue
        if instruction == "RUN":
            for token in tokens[1:]:
                if not token.startswith("--mount="):
                    continue
                mount_options = token.partition("=")[2]
                for option in mount_options.split(","):
                    name, separator, raw_from = option.partition("=")
                    if name != "from" or not separator:
                        continue
                    resolved = _resolve_variables(raw_from, arguments)
                    references.append(
                        _classify(
                            raw_from,
                            resolved,
                            "RUN --mount=from",
                            line_number,
                            identities,
                            frozenset(known_aliases | known_stage_indexes),
                            allowed_short_external_images,
                        )
                    )
    return DockerfileParseResult(
        tuple(references), tuple(aliases), tuple(sorted(set(syntax_errors)))
    )
