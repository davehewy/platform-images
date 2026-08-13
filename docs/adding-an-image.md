# Adding an image

1. Create `images/<name>/Dockerfile`. Names use lowercase letters and digits, optionally separated
   by `.`, `_`, or `-`.
2. Use a logical local target name in `FROM`, `COPY`/`ADD --from`, or `RUN --mount=from` when
   appropriate, for example `FROM base`. Do not add image metadata merely to describe an
   inferable dependency.
   If a new external base uses an unqualified name such as `debian`, add that repository name to
   `dockerfile.allowed_short_external_images` in `platform-images.toml`; alternatively use a
   registry-qualified reference. This explicit distinction catches local dependency typos.
3. Run `platform images validate`.
4. Inspect `platform images graph` and, for automation, `platform images graph --format json`.
5. Build locally with `platform images build <name>`.
6. Commit the new directory and its build inputs.
7. Do **not** edit `.gitlab-ci.yml`; discovery and child-pipeline generation add the job.

The build context is the target directory. Keep scripts, `.containerignore`, and copied files there
unless they are intentionally shared and listed as a global controller input.

An internal namespace reference whose target does not exist, an unresolved build-time-only `ARG`,
or a local dependency cycle fails validation before a build plan can be generated.
