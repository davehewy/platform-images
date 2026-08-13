from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from platform_images import __version__

ARCHITECTURES = {
    "aarch64": "arm64",
    "amd64": "amd64",
    "arm64": "arm64",
    "x86_64": "amd64",
}
SYSTEMS = {"darwin": "darwin", "linux": "linux"}


def native_platform() -> tuple[str, str]:
    system = SYSTEMS.get(platform.system().casefold())
    architecture = ARCHITECTURES.get(platform.machine().casefold())
    if system is None or architecture is None:
        raise RuntimeError(
            f"unsupported standalone build host: {platform.system()} {platform.machine()}"
        )
    return system, architecture


def archive_name(system: str, architecture: str) -> str:
    if system not in SYSTEMS.values() or architecture not in {"amd64", "arm64"}:
        raise ValueError(f"unsupported standalone target: {system}-{architecture}")
    return f"platform-images-{system}-{architecture}.tar.gz"


def _archive(binary: Path, output: Path, root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="platform-images-standalone-") as temporary:
        staging = Path(temporary)
        staged_binary = staging / "platform"
        shutil.copy2(binary, staged_binary)
        staged_binary.chmod(0o755)
        shutil.copy2(root / "LICENSE", staging / "LICENSE")
        shutil.copy2(root / "README.md", staging / "README.md")
        with tarfile.open(output, "w:gz") as bundle:
            for name in ("platform", "LICENSE", "README.md"):
                bundle.add(staging / name, arcname=name)


def build(version: str, expected_system: str, expected_architecture: str, output_dir: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    if version != __version__:
        raise RuntimeError(
            f"requested release version {version!r} does not match package version {__version__!r}"
        )
    actual_system, actual_architecture = native_platform()
    if (actual_system, actual_architecture) != (expected_system, expected_architecture):
        raise RuntimeError(
            "standalone build host does not match requested target: "
            f"requested {expected_system}-{expected_architecture}, "
            f"running {actual_system}-{actual_architecture}"
        )

    pyinstaller_root = root / "build" / "pyinstaller"
    binary_directory = root / "build" / "standalone-binary"
    pyinstaller_root.mkdir(parents=True, exist_ok=True)
    binary_directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--onefile",
            "--name",
            "platform",
            "--distpath",
            str(binary_directory),
            "--workpath",
            str(pyinstaller_root / "work"),
            "--specpath",
            str(pyinstaller_root),
            "src/platform_images/__main__.py",
        ],
        cwd=root,
        check=True,
    )
    binary = binary_directory / "platform"
    if not binary.is_file():
        raise RuntimeError(f"PyInstaller did not create the expected executable: {binary}")

    environment = dict(os.environ)
    environment["PLATFORM_IMAGES_ROOT"] = str(root)
    subprocess.run([binary, "images", "validate"], cwd=root, env=environment, check=True)
    subprocess.run([binary, "images", "graph"], cwd=root, env=environment, check=True)

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / archive_name(expected_system, expected_architecture)
    _archive(binary, output, root)
    print(f"Built platform-images {version}: {output}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a native standalone release archive")
    parser.add_argument("--version", required=True)
    parser.add_argument("--os", required=True, choices=sorted(SYSTEMS.values()))
    parser.add_argument("--arch", required=True, choices=("amd64", "arm64"))
    parser.add_argument("--output-dir", type=Path, default=Path("standalone-dist"))
    arguments = parser.parse_args()
    build(arguments.version, arguments.os, arguments.arch, arguments.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
