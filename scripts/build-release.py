"""Build hook executed inside Python Semantic Release's isolated action container."""

from __future__ import annotations

import subprocess
import sys

UV_VERSION = "0.12.3"


def main() -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", f"uv=={UV_VERSION}"], check=True)
    subprocess.run(["uv", "lock"], check=True)
    subprocess.run(["uv", "build"], check=True)


if __name__ == "__main__":
    main()
