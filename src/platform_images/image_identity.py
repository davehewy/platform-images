from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher


def repository_name(reference: str) -> str:
    """Return an image repository without a tag or digest."""
    without_digest = reference.split("@", 1)[0]
    slash = without_digest.rfind("/")
    colon = without_digest.rfind(":")
    return without_digest[:colon] if colon > slash else without_digest


def registry_relative_repository(reference: str) -> str:
    """Remove a Docker-style registry hostname while retaining the repository path."""
    repository = repository_name(reference)
    first, separator, remainder = repository.partition("/")
    registry_qualified = separator and (first == "localhost" or "." in first or ":" in first)
    return remainder if registry_qualified else repository


def registry_hostname(reference: str) -> str | None:
    """Return an explicit Docker-style registry hostname, including an optional port."""
    repository = repository_name(reference)
    first, separator, _remainder = repository.partition("/")
    if separator and (first == "localhost" or "." in first or ":" in first):
        return first
    return None


def repository_is_within(reference: str, prefix: str) -> bool:
    """Return whether a repository is the prefix itself or one of its descendants."""
    repository = repository_name(reference)
    normalized_prefix = repository_name(prefix).rstrip("/")
    return repository == normalized_prefix or repository.startswith(f"{normalized_prefix}/")


def compact_image_name(value: str) -> str:
    """Return the separator-insensitive key used for strong image-name comparisons."""
    return "".join(character for character in value.casefold() if character.isalnum())


def probable_target_matches(
    reference: str,
    target_names: Iterable[str],
) -> tuple[tuple[str, float], ...]:
    """Rank deterministic local-name candidates for a qualified image reference."""
    basename = repository_name(reference).rsplit("/", 1)[-1]
    compact_basename = compact_image_name(basename)
    scored: list[tuple[float, str]] = []
    for target in target_names:
        score = SequenceMatcher(None, basename, target).ratio()
        if compact_basename == compact_image_name(target):
            score = 1.0
        if score >= 0.75:
            scored.append((score, target))
    ordered = sorted(scored, key=lambda item: (-item[0], item[1]))
    return tuple((target, score) for score, target in ordered)


@dataclass(frozen=True)
class ImageIdentityResolver:
    identities: Mapping[str, frozenset[str]]
    managed_prefixes: frozenset[str]
    target_names: frozenset[str]
    external_repositories: frozenset[str]
    internal_registries: frozenset[str]
    managed_source_prefixes: frozenset[str]
    _probable_matches_cache: dict[str, tuple[tuple[str, float], ...]] = field(
        default_factory=dict,
        init=False,
        compare=False,
        repr=False,
    )

    def candidates(self, reference: str) -> frozenset[str]:
        repository = repository_name(reference)
        relative = registry_relative_repository(repository)
        configured = self.identities.get(repository, frozenset()) | self.identities.get(
            relative, frozenset()
        )
        if configured:
            return configured
        if self.is_explicit_external(repository) or "/" not in repository:
            return frozenset()
        basename = repository.rsplit("/", 1)[-1]
        return frozenset({basename}) if basename in self.target_names else frozenset()

    def is_explicit_external(self, reference: str) -> bool:
        repository = repository_name(reference)
        relative = registry_relative_repository(repository)
        return repository in self.external_repositories or relative in self.external_repositories

    def probable_local_targets(self, reference: str) -> tuple[str, ...]:
        """Return strong, deterministic near-matches for a qualified external reference."""
        repository = repository_name(reference)
        if "/" not in repository or self.is_explicit_external(repository):
            return ()
        cached = self._probable_matches_cache.get(repository)
        if cached is not None:
            return tuple(target for target, _score in cached)
        result = probable_target_matches(repository, self.target_names)
        # Validation commonly sees the same public base image in hundreds of Dockerfiles. The
        # resolver is scoped to one immutable target set, so memoizing by normalized repository
        # avoids repeating an O(targets) fuzzy scan without changing any matching semantics.
        self._probable_matches_cache[repository] = result
        return tuple(target for target, _score in result)

    def probable_local_matches(self, reference: str) -> tuple[tuple[str, float], ...]:
        """Return probable local targets with their deterministic similarity scores."""
        repository = repository_name(reference)
        if "/" not in repository or self.is_explicit_external(repository):
            return ()
        cached = self._probable_matches_cache.get(repository)
        if cached is None:
            # Populate the shared cache through the public target-only view.
            self.probable_local_targets(repository)
            cached = self._probable_matches_cache.get(repository, ())
        return cached

    def is_managed(self, reference: str) -> bool:
        repository = repository_name(reference)
        relative = registry_relative_repository(repository)
        automatically_managed = any(
            candidate.startswith(f"{prefix}/")
            for candidate in {repository, relative}
            for prefix in self.managed_prefixes
        )
        explicitly_managed = self.is_managed_source(repository)
        return automatically_managed or explicitly_managed

    def is_managed_source(self, reference: str) -> bool:
        """Return whether a source is beneath an explicitly owned registry prefix."""
        repository = repository_name(reference)
        return any(
            repository_is_within(repository, prefix) for prefix in self.managed_source_prefixes
        )

    def is_internal_registry(self, reference: str) -> bool:
        """Return whether the source explicitly names a configured internal registry."""
        return registry_hostname(reference) in self.internal_registries

    @property
    def collisions(self) -> Mapping[str, frozenset[str]]:
        return {
            identity: targets for identity, targets in self.identities.items() if len(targets) > 1
        }


def build_image_identity_resolver(
    target_names: Iterable[str],
    namespace: str,
    *,
    repositories: Mapping[str, str] | None = None,
    aliases: Mapping[str, tuple[str, ...]] | None = None,
    external_repositories: Iterable[str] = (),
    internal_registries: Iterable[str] = (),
    managed_repository_prefixes: Iterable[str] = (),
) -> ImageIdentityResolver:
    targets = frozenset(target_names)
    configured_repositories = repositories or {}
    configured_aliases = aliases or {}
    identities: dict[str, set[str]] = {}
    managed_prefixes: set[str] = set()

    def register(identity: str, target: str, *, managed: bool) -> None:
        repository = repository_name(identity)
        relative = registry_relative_repository(repository)
        for candidate in {repository, relative}:
            identities.setdefault(candidate, set()).add(target)
            if managed and "/" in candidate:
                managed_prefixes.add(candidate.rsplit("/", 1)[0])

    for target in sorted(targets):
        register(target, target, managed=False)
        repository = configured_repositories.get(target, f"{namespace.strip('/')}/{target}")
        register(repository, target, managed=True)
        for alias in configured_aliases.get(target, ()):
            register(alias, target, managed=True)

    return ImageIdentityResolver(
        {identity: frozenset(targets) for identity, targets in sorted(identities.items())},
        frozenset(managed_prefixes),
        targets,
        frozenset(repository_name(reference) for reference in external_repositories),
        frozenset(internal_registries),
        frozenset(repository_name(prefix) for prefix in managed_repository_prefixes),
    )
