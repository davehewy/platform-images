from __future__ import annotations

import difflib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from platform_images.config import RepositoryConfig
from platform_images.dockerfile import rewrite_argument_image_references
from platform_images.errors import PlatformImagesError
from platform_images.image_identity import registry_hostname
from platform_images.models import ReferenceKind
from platform_images.validation import ValidationReport, validate_repository


@dataclass(frozen=True)
class ReferenceNormalizationResult:
    changed: bool
    diff: str
    files: tuple[str, ...]
    validation_report: ValidationReport


def _atomic_write(path: Path, text: str, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_text_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def normalize_internal_references(
    config: RepositoryConfig,
    *,
    write: bool = False,
) -> ReferenceNormalizationResult:
    """Preview or apply qualified-local to logical-local Dockerfile reference rewrites."""
    before_report = validate_repository(config)
    if not before_report.valid:
        raise PlatformImagesError(
            "reference normalization requires a valid repository configuration"
        )

    changes: dict[Path, tuple[str, str, int]] = {}
    diff_parts: list[str] = []
    for target_name in sorted(before_report.graph.targets):
        target = before_report.graph.targets[target_name]
        replacements: dict[str, str] = {}
        for reference in before_report.graph.parse_results[target_name].references:
            if (
                reference.kind is not ReferenceKind.LOCAL_TARGET
                or reference.resolved is None
                or "$" in reference.raw
            ):
                continue
            source = reference.source or reference.raw
            if registry_hostname(source) not in config.identity.internal_registries:
                continue
            replacements[reference.raw] = reference.resolved
        if not replacements:
            continue
        try:
            before = _read_text_exact(target.dockerfile)
            mode = stat.S_IMODE(target.dockerfile.stat().st_mode)
        except (OSError, UnicodeDecodeError) as exc:
            raise PlatformImagesError(
                f"unable to read build file for reference normalization: {target.dockerfile}: {exc}"
            ) from exc
        after, replaced = rewrite_argument_image_references(before, replacements)
        if replaced != frozenset(replacements):
            missing = sorted(set(replacements) - replaced)
            raise PlatformImagesError(
                f"cannot safely normalize every local reference in {target.dockerfile}: "
                + ", ".join(missing)
            )
        if before == after:
            continue
        relative = target.dockerfile.relative_to(config.root).as_posix()
        changes[target.dockerfile] = (before, after, mode)
        diff_parts.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )

    result_report = before_report
    if write and changes:
        written: list[Path] = []
        try:
            for path, (before, _after, _mode) in changes.items():
                if _read_text_exact(path) != before:
                    raise PlatformImagesError(
                        f"build file changed during reference normalization: {path}"
                    )
            for path, (_before, after, mode) in changes.items():
                _atomic_write(path, after, mode)
                written.append(path)
            result_report = validate_repository(RepositoryConfig.load(config.root))
            if not result_report.valid:
                details = "; ".join(
                    f"[{issue.code}] {issue.message}" for issue in result_report.errors[:3]
                )
                raise PlatformImagesError(
                    "normalized references would make repository validation fail: " + details
                )
            if result_report.graph.dependencies != before_report.graph.dependencies:
                raise PlatformImagesError(
                    "normalized references changed the resolved dependency graph"
                )
        except Exception as exc:
            for path in written:
                before, _after, mode = changes[path]
                _atomic_write(path, before, mode)
            if isinstance(exc, PlatformImagesError) and written:
                raise PlatformImagesError(f"{exc}; build files were restored") from exc
            if isinstance(exc, PlatformImagesError):
                raise
            raise PlatformImagesError(
                f"reference normalization failed and build files were restored: {exc}"
            ) from exc

    return ReferenceNormalizationResult(
        bool(changes),
        "".join(diff_parts),
        tuple(path.relative_to(config.root).as_posix() for path in changes),
        result_report,
    )
