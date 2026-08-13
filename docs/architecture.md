# Architecture

The controller is intentionally thin. Podman builds, GitLab schedules, and ECR stores images.
The Python package makes repository conventions explicit and testable.

## Data flow

1. **Discovery** maps every direct `images/<name>` directory to an immutable `ImageTarget`.
2. **Parser** reads logical Dockerfile instructions, global `ARG` defaults, `FROM` sources, stage
   aliases, `COPY`/`ADD --from` sources, and `RUN --mount=from` sources while excluding heredoc
   bodies. It distinguishes local, external, stage, and unresolved references.
3. **Graph** is authoritative as `dependencies[consumer]` and `dependents[input]`. JSON is the
   machine contract; the tree is only a human projection and repeats multi-parent DAG nodes.
4. **Validation** rejects unsafe names and paths, missing Dockerfiles, unresolved/internal
   references, unapproved short external names, stage-name collisions, and complete deterministic
   cycle paths.
5. **Changes** parses NUL-delimited, rename-aware Git output and maps image paths or global inputs
   to directly changed targets. Controller, lock, configuration, and pipeline paths are mandatory
   global inputs in code, so a configuration edit cannot disable its own rebuild. Deleted targets
   are reported, never deleted from ECR.
6. **Affected calculation** follows downstream dependents from directly changed images.
7. **Planner** topologically orders only the rebuild set and resolves every output, dependency
   input, reason, and in-plan `needs` edge without building anything.
8. **Executor** translates plan targets into argument-array Podman calls. Logical dependencies are
   bound through `--build-context name=container-image://reference`.
9. **GitLab renderer** strictly reloads the saved JSON plan, verifies it against the discovered
   graph, and safely serializes jobs. Direct dependencies become `needs`; an empty plan becomes a
   successful no-op job.
10. **Registry adapter** resolves stable ECR tags in the account and region encoded by the registry
    hostname, pushes with `--digestfile`, and gates stable promotion behind one graph-wide job.

Core graph, change, planning, and rendering code never invokes Git, Podman, AWS, or the registry.
Those processes sit behind small injectable adapters so unit tests use recorded inputs.

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
