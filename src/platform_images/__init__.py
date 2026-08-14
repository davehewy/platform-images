"""Convention-driven container image controller."""

try:
    from platform_images._version import __version__
except ImportError:  # pragma: no cover - only an uninstalled source tree lacks the build hook
    __version__ = "0+unknown"
