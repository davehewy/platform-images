# Architecture

The controller is intentionally thin. A supported container backend builds, GitHub Actions or
GitLab schedules, and ECR stores images. The Python package makes repository conventions explicit
and testable.

## Data flow

1. **Discovery** maps every direct child of configurable `discovery.root` to an immutable
   `ImageTarget`. The default root is `images`, but nested repository-relative roots are supported.
2. **Parser** reads logical Dockerfile/Containerfile instructions, global `ARG` defaults, `FROM` sources, stage
   aliases, `COPY`/`ADD --from` sources, and `RUN --mount=from` sources while excluding heredoc
   bodies. It distinguishes local, external, stage, and unresolved references.
3. **Graph** is authoritative as `dependencies[consumer]` and `dependents[input]`. JSON is the
   machine contract; the tree is only a human projection and repeats multi-parent DAG nodes.
4. **Validation** rejects unsafe names and paths, missing or ambiguous build files, unresolved/internal
   references, unapproved short external names, stage-name collisions, and complete deterministic
   cycle paths.
5. **Changes** parses NUL-delimited, rename-aware Git output and maps image paths or global inputs
   to directly changed targets. Controller, lock, configuration, and pipeline paths are mandatory
   global inputs in code, so a configuration edit cannot disable its own rebuild. Deleted targets
   are reported, never deleted from ECR.
6. **Affected calculation** follows downstream dependents from directly changed images.
7. **Planner** topologically orders only the rebuild set and resolves every output, dependency
   input, reason, and in-plan `needs` edge without building anything.
8. **Executor** translates plan targets into argument-array Docker Buildx, Podman, Buildah, or
   nerdctl calls. Logical dependencies are bound through exact named `docker-image://` or
   `container-image://` contexts.
9. **GitLab renderer** strictly reloads the saved JSON plan, verifies it against the discovered
   graph, and safely serializes jobs. Direct dependencies become `needs`; an empty plan becomes a
   successful no-op job.
10. **GitHub renderer** generates static graph-depth jobs whose runtime matrices contain only the
    dependency-safe layers of the affected plan. The saved plan is passed as an artifact and
    strictly revalidated in every matrix build.
11. **Registry adapter** resolves stable ECR tags in the account and region encoded by the registry
    hostname. Builder capabilities determine direct or local output; transport capabilities own
    authentication, inspection, digest capture, and promotion behind one graph-wide job.

Core graph, change, planning, and rendering code never invokes Git, container tooling, AWS, or the
registry. Those processes sit behind small injectable adapters so unit tests use recorded inputs.

## DAG invariant

Edges are directed from a consumer to each build-time input in `dependencies`, with the inverse
stored in `dependents`. The graph must be acyclic. Validation performs deterministic depth-first
cycle detection and reports the closed path; planning refuses to continue until the cycle is
removed. Topological ordering reverses the dependency constraint for execution, ensuring every
selected input completes before its consumer. GitLab expresses direct selected edges with `needs`;
GitHub groups the same partial DAG into dependency-safe parallel layers.

## Reference policy

| Context | Output |
| --- | --- |
| Local | `localhost/platform-images/<target>:dev` |
| Merge request | `<registry>/platform-images/<target>:ci-<pipeline>-<commit>` |
| Default branch | `<registry>/platform-images/<target>:sha-<full-commit>` |
| Stable input/alias | `<registry>/platform-images/<target>:main` |

Dependencies built in the same plan use that plan's unique output. Dependencies outside a partial
plan resolve `main` through ECR and are injected by immutable digest. A missing stable dependency
fails the plan; the controller never silently builds unrelated upstream images.

Unqualified image repositories are ambiguous because Docker normally interprets both `FROM base`
and `FROM alpine` as short registry names. The controller resolves a discovered target as local,
accepts `scratch`, and accepts only the additional external short names explicitly listed in
`dockerfile.allowed_short_external_images`. All other unqualified names fail validation. A
registry-qualified reference remains external unless it lies beneath the configured internal
namespace.
