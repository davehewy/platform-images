from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from pathlib import Path

from platform_images.config import RepositoryConfig
from platform_images.errors import GitError, PlatformImagesError, ProcessError
from platform_images.graph import ImageGraph
from platform_images.models import ChangedPath, ChangeSet, ImageTarget, ReferenceKind
from platform_images.process import ProcessRunner

ZERO_SHA = "0" * 40

# These inputs define discovery, graph construction, selection, and CI rendering. They are
# deliberately not configurable: a commit must not be able to disable rebuild detection for the
# same commit by editing platform-images.toml.
MANDATORY_GLOBAL_INPUTS = (
    ".github/workflows/**",
    ".gitlab-ci.yml",
    ".gitlab/**",
    "platform-images.toml",
    "pyproject.toml",
    "src/platform_images/**",
    "uv.lock",
)


class GitClient:
    def __init__(self, root: Path, runner: ProcessRunner | None = None) -> None:
        self.root = root
        self.runner = runner or ProcessRunner()

    def changed_paths(self, base: str, head: str) -> tuple[ChangedPath, ...]:
        try:
            result = self.runner.run(
                ["git", "diff", "--name-status", "--find-renames", "-z", base, head],
                cwd=self.root,
                capture_output=True,
            )
        except ProcessError as exc:
            raise GitError(f"unable to compare Git commits {base!r} and {head!r}: {exc}") from exc
        return parse_name_status(result.stdout)

    def merge_base(self, left: str, right: str) -> str:
        try:
            result = self.runner.run(
                ["git", "merge-base", left, right], cwd=self.root, capture_output=True
            )
        except ProcessError as exc:
            raise GitError(f"unable to determine merge base for {left} and {right}: {exc}") from exc
        value = result.stdout.strip()
        if not value:
            raise GitError(f"Git returned an empty merge base for {left} and {right}")
        return value


def parse_name_status(output: str) -> tuple[ChangedPath, ...]:
    fields = output.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    changes: list[ChangedPath] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        kind = status[:1]
        if kind in {"R", "C"}:
            if index + 1 >= len(fields):
                raise GitError("malformed NUL-delimited Git rename/copy output")
            old_path, new_path = fields[index], fields[index + 1]
            index += 2
        else:
            if index >= len(fields):
                raise GitError("malformed NUL-delimited Git diff output")
            path = fields[index]
            index += 1
            old_path = path if kind == "D" else None
            new_path = None if kind == "D" else path
        changes.append(ChangedPath(status, old_path, new_path))
    return tuple(sorted(changes, key=lambda item: (item.paths, item.status)))


def _matches(path: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/**") and path.startswith(pattern[:-3].rstrip("/") + "/"):
            return True
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def _target_for_path(path: str) -> str | None:
    parts = Path(path).parts
    if len(parts) >= 3 and parts[0] == "images":
        return parts[1]
    return None


def map_changes(
    changed_files: tuple[ChangedPath, ...],
    targets: dict[str, ImageTarget] | object,
    config: RepositoryConfig,
) -> ChangeSet:
    target_names = set(targets)  # type: ignore[arg-type]
    reasons: dict[str, set[str]] = {}
    removed: set[str] = set()
    global_paths: set[str] = set()
    global_inputs = tuple(sorted(set(MANDATORY_GLOBAL_INPUTS).union(config.changes.global_inputs)))

    for change in changed_files:
        for path in change.paths:
            if _matches(path, global_inputs):
                global_paths.add(path)
            target_name = _target_for_path(path)
            if target_name is None:
                continue
            if target_name in target_names:
                reasons.setdefault(target_name, set()).add(f"source-changed:{path}")
            elif change.status.startswith(("D", "R")) and path.endswith(
                ("/Dockerfile", "/Containerfile")
            ):
                removed.add(target_name)
    if global_paths:
        for target_name in target_names:
            for path in global_paths:
                reasons.setdefault(target_name, set()).add(f"global-input:{path}")
    stable_reasons = {name: tuple(sorted(values)) for name, values in sorted(reasons.items())}
    return ChangeSet(
        changed_files=changed_files,
        changed_targets=frozenset(stable_reasons),
        removed_targets=frozenset(sorted(removed - target_names)),
        global_change=bool(global_paths),
        reasons=stable_reasons,
    )


def detect_changes(
    config: RepositoryConfig,
    targets: dict[str, ImageTarget] | object,
    base: str,
    head: str,
    git: GitClient | None = None,
) -> ChangeSet:
    client = git or GitClient(config.root)
    return map_changes(client.changed_paths(base, head), targets, config)


def validate_removed_references(graph: ImageGraph, changes: ChangeSet) -> None:
    """Reject surviving bare references to targets deleted by this comparison."""
    for consumer in sorted(graph.targets):
        for reference in graph.parse_results[consumer].references:
            if reference.kind not in {
                ReferenceKind.EXTERNAL_IMAGE,
                ReferenceKind.UNRESOLVED,
            }:
                continue
            repository = reference.raw.split("@", 1)[0]
            if "/" not in repository and ":" in repository:
                repository = repository.split(":", 1)[0]
            if repository in changes.removed_targets:
                target = graph.targets[consumer]
                path = target.dockerfile.relative_to(target.directory.parents[1]).as_posix()
                raise PlatformImagesError(
                    f"{path}:{reference.line_number} still references removed local image target "
                    f'"{repository}"'
                )
