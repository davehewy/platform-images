"""Build hook executed inside Python Semantic Release's isolated action container."""

from __future__ import annotations

import os
import subprocess
import sys

UV_VERSION = "0.12.3"


def main() -> None:
    version = os.environ.get("NEW_VERSION")
    if not version:
        raise SystemExit("NEW_VERSION is required for a semantic release build")

    build_environment = os.environ.copy()
    # hatch-vcs 0.5.0 consumes setuptools-scm's documented generic override.
    # The release action builds only this distribution in its isolated environment.
    build_environment["SETUPTOOLS_SCM_PRETEND_VERSION"] = version
    subprocess.run([sys.executable, "-m", "pip", "install", f"uv=={UV_VERSION}"], check=True)
    subprocess.run(["uv", "lock"], check=True, env=build_environment)
    subprocess.run(["uv", "build"], check=True, env=build_environment)


if __name__ == "__main__":
    main()
