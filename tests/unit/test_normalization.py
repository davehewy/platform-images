from __future__ import annotations

from pathlib import Path

import pytest

from platform_images.cli import main
from platform_images.config import RepositoryConfig
from platform_images.errors import PlatformImagesError
from platform_images.normalization import normalize_internal_references
from platform_images.validation import validate_repository


def _internal_repository(root: Path) -> Path:
    targets = root / "containers"
    definitions = {
        "base": "FROM alpine:3.22\n",
        "tooling": "FROM alpine:3.22\n",
        "application": (
            "FROM nexus.internal:8088/some-repo/sub-repo/base:v1.1\n"
            "COPY --from=nexus.internal:8088/some-repo/sub-repo/tooling:v2 /tool /tool\n"
            "COPY --from=nexus.internal:8088/some-repo/sub-repo/vendor-sdk:v4 /sdk /sdk\n"
        ),
    }
    for name, dockerfile in definitions.items():
        directory = targets / name
        directory.mkdir(parents=True)
        (directory / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    return targets / "application" / "Dockerfile"


def test_normalize_references_previews_checks_and_atomically_applies(
    tmp_path: Path, capsys
) -> None:
    application = _internal_repository(tmp_path)
    assert main(["init"], cwd=tmp_path, environment={}) == 0
    capsys.readouterr()
    before = application.read_text(encoding="utf-8")

    assert main(["images", "normalize-references"], cwd=tmp_path, environment={}) == 0
    preview = capsys.readouterr().out
    assert "-FROM nexus.internal:8088/some-repo/sub-repo/base:v1.1" in preview
    assert "+FROM base" in preview
    assert "Preview only" in preview
    assert application.read_text(encoding="utf-8") == before

    assert (
        main(
            ["images", "normalize-references", "--check"],
            cwd=tmp_path,
            environment={},
        )
        == 1
    )
    capsys.readouterr()
    assert application.read_text(encoding="utf-8") == before

    assert (
        main(
            ["images", "normalize-references", "--apply"],
            cwd=tmp_path,
            environment={},
        )
        == 0
    )
    output = capsys.readouterr().out
    normalized = application.read_text(encoding="utf-8")
    assert "FROM base\n" in normalized
    assert "COPY --from=tooling /tool /tool\n" in normalized
    assert "nexus.internal:8088/some-repo/sub-repo/vendor-sdk:v4" in normalized
    assert "Validation passed after normalization." in output
    report = validate_repository(RepositoryConfig.load(tmp_path))
    assert report.valid
    assert report.graph.direct_dependencies("application") == ("base", "tooling")


def test_normalize_references_does_not_rewrite_other_registries_or_arg_sources(
    tmp_path: Path, capsys
) -> None:
    targets = tmp_path / "containers"
    for name, dockerfile in {
        "base": "FROM alpine:3.22\n",
        "tooling": "FROM alpine:3.22\n",
        "application": (
            "ARG SOURCE=nexus.internal:8088/team/images\n"
            "FROM ${SOURCE}/base:v1\n"
            "COPY --from=ghcr.io/acme/tooling:v2 /tool /tool\n"
        ),
    }.items():
        directory = targets / name
        directory.mkdir(parents=True)
        (directory / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    assert main(["init"], cwd=tmp_path, environment={}) == 0
    capsys.readouterr()
    application = targets / "application" / "Dockerfile"
    before = application.read_text(encoding="utf-8")

    assert main(["images", "normalize-references"], cwd=tmp_path, environment={}) == 0

    assert application.read_text(encoding="utf-8") == before
    assert "already normalized" in capsys.readouterr().out


def test_normalize_references_preserves_crlf_line_endings(tmp_path: Path, capsys) -> None:
    application = _internal_repository(tmp_path)
    text = application.read_text(encoding="utf-8")
    with application.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text.replace("\n", "\r\n"))
    assert main(["init"], cwd=tmp_path, environment={}) == 0
    capsys.readouterr()

    assert main(["images", "normalize-references", "--apply"], cwd=tmp_path, environment={}) == 0

    with application.open("r", encoding="utf-8", newline="") as stream:
        normalized = stream.read()
    assert "FROM base\r\n" in normalized
    assert "COPY --from=tooling /tool /tool\r\n" in normalized
    assert normalized.count("\r\n") == 3


def test_normalize_references_refuses_a_concurrent_build_file_edit(
    tmp_path: Path, monkeypatch
) -> None:
    application = _internal_repository(tmp_path)
    assert main(["init"], cwd=tmp_path, environment={}) == 0
    config = RepositoryConfig.load(tmp_path)
    original_read = Path.open
    reads = 0

    def editing_open(path: Path, *args: object, **kwargs: object):
        nonlocal reads
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == application and mode == "r":
            reads += 1
            if reads == 3:
                with original_read(path, "a", encoding="utf-8", newline="") as stream:
                    stream.write("# concurrent edit\n")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", editing_open)

    try:
        with pytest.raises(PlatformImagesError, match="changed during reference normalization"):
            normalize_internal_references(config, write=True)
    finally:
        monkeypatch.setattr(Path, "open", original_read)
    assert application.read_text(encoding="utf-8").endswith("# concurrent edit\n")
