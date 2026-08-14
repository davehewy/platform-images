from __future__ import annotations

from dataclasses import dataclass

from platform_images.config import RepositoryConfig
from platform_images.models import BuildMode


@dataclass(frozen=True)
class ReferencePolicy:
    config: RepositoryConfig
    registry: str | None = None
    pipeline_id: str | None = None

    def local(self, target: str) -> str:
        return f"localhost/{self.config.image_repository(target)}:dev"

    def output(self, target: str, mode: BuildMode, commit_sha: str) -> str:
        if mode is BuildMode.LOCAL:
            return self.local(target)
        registry = self._registry()
        if mode is BuildMode.MERGE_REQUEST:
            if not self.pipeline_id:
                raise ValueError("pipeline ID is required for merge-request image references")
            tag = f"{self.config.tags.ci_prefix}-{self.pipeline_id}-{commit_sha}"
        else:
            tag = f"{self.config.tags.commit_prefix}-{commit_sha}"
        return f"{registry}/{self.config.image_repository(target)}:{tag}"

    def stable(self, target: str) -> str:
        return (
            f"{self._registry()}/{self.config.image_repository(target)}:"
            f"{self.config.registry.stable_tag}"
        )

    def _registry(self) -> str:
        if not self.registry:
            raise ValueError(
                f"registry is required; set {self.config.registry.registry_environment_variable}"
            )
        return self.registry.rstrip("/")


def immutable_reference(reference: str, digest: str) -> str:
    last_slash = reference.rfind("/")
    last_colon = reference.rfind(":")
    repository = reference[:last_colon] if last_colon > last_slash else reference
    return f"{repository}@{digest}"
