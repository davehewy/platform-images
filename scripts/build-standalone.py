from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import zipfile
from pathlib import Path

from platform_images import __version__

ARCHITECTURES = {
    "aarch64": "arm64",
    "amd64": "amd64",
    "arm64": "arm64",
    "x86_64": "amd64",
}
SYSTEMS = {"darwin": "darwin", "linux": "linux", "windows": "windows"}
WINDOWS_INTERPRETER_ARCHITECTURES = {
    "win-amd64": "amd64",
    "win-arm64": "arm64",
}
RELEASE_VERSION_RE = re.compile(r"^[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*$")


def native_platform() -> tuple[str, str]:
    system = SYSTEMS.get(platform.system().casefold())
    if system == "windows":
        architecture = WINDOWS_INTERPRETER_ARCHITECTURES.get(sysconfig.get_platform().casefold())
    else:
        architecture = ARCHITECTURES.get(platform.machine().casefold())
    if system is None or architecture is None:
        raise RuntimeError(
            f"unsupported standalone build host: {platform.system()} {platform.machine()}"
        )
    return system, architecture


def archive_name(version: str, system: str, architecture: str) -> str:
    if system not in SYSTEMS.values() or architecture not in {"amd64", "arm64"}:
        raise ValueError(f"unsupported standalone target: {system}-{architecture}")
    normalized_version = version.removeprefix("v")
    if not RELEASE_VERSION_RE.fullmatch(normalized_version):
        raise ValueError(f"invalid standalone release version: {version!r}")
    suffix = ".zip" if system == "windows" else ".tar.gz"
    return f"platform-images-v{normalized_version}-{system}-{architecture}{suffix}"


def _archive(binary: Path, output: Path, root: Path, system: str) -> None:
    with tempfile.TemporaryDirectory(prefix="platform-images-standalone-") as temporary:
        staging = Path(temporary)
        binary_name = "platform.exe" if system == "windows" else "platform"
        staged_binary = staging / binary_name
        shutil.copy2(binary, staged_binary)
        if system != "windows":
            staged_binary.chmod(0o755)
        shutil.copy2(root / "LICENSE", staging / "LICENSE")
        shutil.copy2(root / "README.md", staging / "README.md")
        names = (binary_name, "LICENSE", "README.md")
        if system == "windows":
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for name in names:
                    bundle.write(staging / name, arcname=name)
        else:
            with tarfile.open(output, "w:gz") as bundle:
                for name in names:
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
    binary_name = "platform.exe" if expected_system == "windows" else "platform"
    binary = binary_directory / binary_name
    if not binary.is_file():
        raise RuntimeError(f"PyInstaller did not create the expected executable: {binary}")

    environment = dict(os.environ)
    environment["PLATFORM_IMAGES_ROOT"] = str(root)
    subprocess.run([binary, "images", "validate"], cwd=root, env=environment, check=True)
    subprocess.run([binary, "images", "graph"], cwd=root, env=environment, check=True)

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / archive_name(version, expected_system, expected_architecture)
    _archive(binary, output, root, expected_system)
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
