from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2]


def load_release_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "platform_images_release_builder", ROOT / "scripts" / "build-release.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_build_requires_semantic_version(monkeypatch: pytest.MonkeyPatch) -> None:
    builder = load_release_builder()
    monkeypatch.delenv("NEW_VERSION", raising=False)

    with pytest.raises(SystemExit, match="NEW_VERSION is required"):
        builder.main()


def test_release_build_pins_vcs_version_for_lock_and_distributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = load_release_builder()
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("NEW_VERSION", "1.2.3")
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    builder.main()

    assert [command[-2:] for command, _ in calls] == [
        ["install", f"uv=={builder.UV_VERSION}"],
        ["uv", "lock"],
        ["uv", "build"],
    ]
    for _, kwargs in calls[1:]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["SETUPTOOLS_SCM_PRETEND_VERSION"] == "1.2.3"
