# Adding an image

1. Beneath any configured `discovery.roots` entry, create a direct target directory containing
   exactly one `Dockerfile` or `Containerfile`. Both use the same instruction syntax. Target names
   use lowercase letters and digits, optionally separated by `.`, `_`, or `-`, and must remain
   unique across every configured root.
2. Use a logical local target name in `FROM`, `COPY`/`ADD --from`, or `RUN --mount=from` when
   appropriate, for example `FROM base`. Do not add image metadata merely to describe an
   inferable dependency.
   Prefer that short logical name for images owned by the same repository. An existing qualified
   reference is also automatic when its final repository component exactly matches a unique local
   target, regardless of registry hostname, intermediate path, tag, or digest. Use
   `[images.<target>].repository` or a tagless `aliases` entry only when the local and remote names
   differ.
   `platform images validate` prints the exact mapping to add for strong near-matches.
   If a qualified source is assembled from a global `ARG` without a default, configure its
   deterministic value under `[dockerfile.arguments]`. The controller uses it for graph parsing and
   passes the same value to the backend as `--build-arg`, so discovery and execution cannot drift.
   Run builds through `platform images build` or generated CI so those named contexts are present;
   a plain container build command may try to pull the logical name.
   If a new external base uses an unqualified name such as `debian`, add that repository name to
   `dockerfile.allowed_short_external_images` in `platform-images.toml`; alternatively use a
   registry-qualified reference. This explicit distinction catches local dependency typos.
3. Run `platform images validate`.
4. Inspect `platform images graph` and, for automation, `platform images graph --format json`.
5. Build locally with `platform images build <name>`. Use the configured backend or override it
   with `--builder docker`, `podman`, `buildah`, or `nerdctl`.
6. Commit the new directory and its build inputs.
7. Do **not** add a hand-maintained per-image CI job. GitLab child-pipeline generation discovers it;
   for GitHub Actions, regenerate the checked-in workflow so its maximum static depth stays current.

The build context is the target directory. Keep scripts, `.containerignore`, and copied files there
unless they are intentionally shared and listed as a global controller input.

An internal repository reference whose target does not exist, an identity claimed by multiple
targets, an unresolved build-time-only `ARG`, or a local dependency cycle fails validation before a
build plan can be generated. The unresolved-ARG error prints the `[dockerfile.arguments]` remedy. A
qualified near-match is reported as a warning and remains external until it is mapped; add it to
`identity.external_repositories` instead when that is intentional.
