from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ReferenceKind(StrEnum):
    LOCAL_TARGET = "local_target"
    EXTERNAL_IMAGE = "external_image"
    STAGE_ALIAS = "stage_alias"
    UNRESOLVED = "unresolved"


class BuildMode(StrEnum):
    LOCAL = "local"
    MERGE_REQUEST = "merge_request"
    DEFAULT_BRANCH = "default_branch"


class BuildEngine(StrEnum):
    PODMAN = "podman"
    DOCKER = "docker"


@dataclass(frozen=True)
class ImageTarget:
    name: str
    directory: Path
    dockerfile: Path
    context: Path


@dataclass(frozen=True)
class ImageReference:
    raw: str
    resolved: str | None
    instruction: str
    line_number: int
    kind: ReferenceKind
    stage_alias: str | None = None


@dataclass(frozen=True)
class ChangedPath:
    status: str
    old_path: str | None
    new_path: str | None

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(path for path in (self.old_path, self.new_path) if path is not None)


@dataclass(frozen=True)
class ChangeSet:
    changed_files: tuple[ChangedPath, ...]
    changed_targets: frozenset[str]
    removed_targets: frozenset[str]
    global_change: bool
    reasons: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class BuildPlanTarget:
    name: str
    reasons: tuple[str, ...]
    dependencies: tuple[str, ...]
    needs: tuple[str, ...]
    dockerfile: str
    context: str
    output_ref: str
    input_refs: Mapping[str, str]
    push: bool


@dataclass(frozen=True)
class BuildPlan:
    schema_version: int
    mode: BuildMode
    base_sha: str | None
    head_sha: str
    targets: tuple[BuildPlanTarget, ...]
    removed_targets: tuple[str, ...]
