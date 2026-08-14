# Architecture

The controller is intentionally thin. A supported container backend builds, GitHub Actions or
GitLab schedules, and ECR stores images. The Python package makes repository conventions explicit
and testable.

## Data flow

1. **Discovery** maps each direct child containing a `Dockerfile` or `Containerfile` beneath a
   configured `discovery.roots` entry to an immutable `ImageTarget`; other directories are ignored.
   The default root is `["images"]`, but roots may be nested anywhere within the repository.
   Logical target names are global and case-insensitively unique across all roots.
2. **Parser** reads logical Dockerfile/Containerfile instructions, global `ARG` defaults, `FROM` sources, stage
   aliases, `COPY`/`ADD --from` sources, and `RUN --mount=from` sources while excluding heredoc
   bodies. It distinguishes local, external, stage, and unresolved references.
3. **Graph** is authoritative as `dependencies[consumer]` and `dependents[input]`. JSON is the
   machine contract; the tree is only a human projection and repeats multi-parent DAG nodes.
4. **Validation** rejects unsafe names and paths, ambiguous build files, missing discovery roots,
   unresolved/internal references, unapproved short external names, stage-name collisions, and
   complete deterministic cycle paths.
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
   graph, and safely serializes jobs. Direct dependencies become `needs`; every plan, including an
   empty one, ends in a commit manifest artifact and exposes a `consume` stage for project tests.
10. **GitHub renderer** generates static graph-depth jobs whose runtime matrices contain only the
    dependency-safe layers of the affected plan. The saved plan is passed as an artifact and
    strictly revalidated in every matrix build.
11. **Manifest verifier** joins per-target results only when their commit, source, output, digest,
    dependency inputs, and expected target set agree with the plan. The resulting JSON is the
    downstream and release bill of materials.
12. **Registry adapter** resolves stable ECR tags in the account and region encoded by the registry
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

The main graph operations are designed around edges rather than repeated whole-graph scans.
Inverse adjacency is built in `O(V + E)`, deterministic topological ordering uses a heap in
`O((V + E) log V)`, and affected propagation is `O(V + E)` over the reachable subgraph. A local
plan computes its upstream closure once, then propagates selected-consumer reasons in one reverse
topological pass. `scripts/benchmark-graph.py` exercises these paths with a configurable synthetic
multi-root DAG; CI guards the 100-image deep-leaf case against algorithmic regressions.

## Reference policy

| Context | Output |
| --- | --- |
| Local | `localhost/platform-images/<target>:dev` |
| Merge request | `<registry>/platform-images/<target>:ci-<pipeline>-<commit>` |
| Default branch | `<registry>/platform-images/<target>:sha-<full-commit>` |
| Stable input/alias | `<registry>/platform-images/<target>:main` |
| Semantic release | `<registry>/platform-images/<target>:v<semver>` |

Dependencies built in the same plan use that plan's unique output. Dependencies outside a partial
plan resolve `main` through ECR and are injected by immutable digest. A missing stable dependency
fails the plan; the controller never silently builds unrelated upstream images.

The build result resolves every commit-addressed output to its registry digest. Test and deployment
jobs consume the manifest's `@sha256` value. A semantic release promotes that same digest to a
human version tag; it does not execute a second build. Commit and semantic tags should be protected
as immutable by registry policy, while a channel such as `main` is deliberately movable.

Unqualified image repositories are ambiguous because Docker normally interprets both `FROM base`
and `FROM alpine` as short registry names. The controller resolves a discovered target as local,
accepts `scratch`, and accepts only the additional external short names explicitly listed in
`dockerfile.allowed_short_external_images`. All other unqualified names fail validation. A
registry-qualified reference remains external unless it lies beneath the configured internal
namespace.
