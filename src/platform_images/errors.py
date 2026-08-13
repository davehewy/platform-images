"""Controller-specific errors with messages suitable for CLI users."""


class PlatformImagesError(Exception):
    """Base class for expected controller failures."""


class ConfigurationError(PlatformImagesError):
    """The repository configuration is invalid."""


class DiscoveryError(PlatformImagesError):
    """Image targets cannot be discovered safely."""


class GraphCycleError(PlatformImagesError):
    def __init__(self, cycle: tuple[str, ...]) -> None:
        self.cycle = cycle
        super().__init__("local image dependency cycle detected:\n" + " -> ".join(cycle))


class GitError(PlatformImagesError):
    """Git could not provide a trustworthy comparison."""


class MissingStableImageError(PlatformImagesError):
    def __init__(self, target: str) -> None:
        self.target = target
        super().__init__(
            f'No stable image exists for dependency "{target}".\n\n'
            "Run a complete default-branch bootstrap build before attempting\n"
            "a partial affected-image build."
        )


class RegistryError(PlatformImagesError):
    """A registry lookup failed for a reason other than a missing image."""


class ProcessError(PlatformImagesError):
    """An external command failed."""
