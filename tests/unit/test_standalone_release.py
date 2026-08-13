from __future__ import annotations

import importlib.util
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2]


def load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "platform_images_standalone_builder", ROOT / "scripts" / "build-standalone.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("system", "architecture", "expected"),
    [
        ("linux", "amd64", "platform-images-linux-amd64.tar.gz"),
        ("linux", "arm64", "platform-images-linux-arm64.tar.gz"),
        ("darwin", "amd64", "platform-images-darwin-amd64.tar.gz"),
        ("darwin", "arm64", "platform-images-darwin-arm64.tar.gz"),
        ("windows", "amd64", "platform-images-windows-amd64.zip"),
        ("windows", "arm64", "platform-images-windows-arm64.zip"),
    ],
)
def test_archive_names_match_release_contract(
    system: str, architecture: str, expected: str
) -> None:
    assert load_builder().archive_name(system, architecture) == expected


def test_posix_archive_contains_executable_documentation_and_license(tmp_path: Path) -> None:
    builder = load_builder()
    root = tmp_path / "root"
    root.mkdir()
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (root / "README.md").write_text("# Read me\n", encoding="utf-8")
    binary = tmp_path / "binary"
    binary.write_bytes(b"executable")
    output = tmp_path / "release.tar.gz"

    builder._archive(binary, output, root, "linux")

    with tarfile.open(output, "r:gz") as archive:
        assert archive.getnames() == ["platform", "LICENSE", "README.md"]
        assert archive.getmember("platform").mode & 0o111


def test_windows_archive_contains_exe_documentation_and_license(tmp_path: Path) -> None:
    builder = load_builder()
    root = tmp_path / "root"
    root.mkdir()
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (root / "README.md").write_text("# Read me\n", encoding="utf-8")
    binary = tmp_path / "platform.exe"
    binary.write_bytes(b"executable")
    output = tmp_path / "release.zip"

    builder._archive(binary, output, root, "windows")

    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["platform.exe", "LICENSE", "README.md"]
        assert archive.read("platform.exe") == b"executable"
