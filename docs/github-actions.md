# GitHub Actions

Generate a complete workflow from the validated image graph:

```bash
platform images generate-workflow github \
  --default-branch main \
  --builder docker \
  --registry-transport docker \
  --output .github/workflows/container-images.yml
```

Commit the generated file. Run the command again whenever a `Dockerfile` or `Containerfile` changes
the depth of the local dependency graph, when the default branch or runner changes, or when moving
between container backends. The generated header records that contract.

## Why the workflow uses layers

GitHub Actions can create a dynamic matrix from an earlier job's output, but a job's `needs`
relationships are part of the workflow definition before the run starts. A single flat matrix
would allow a consumer to race its newly rebuilt parent. The generated workflow therefore contains
one static job per maximum graph depth:

```text
validate -> plan -> image_layer_0 -> image_layer_1 -> ... -> manifest -> promote
```

At runtime, `platform images github-matrix` groups only the affected plan into build-safe waves.
All images within a wave have no dependency on one another and run in parallel. The next wave waits
for the previous wave, so every selected parent has pushed its exact output before a consumer
starts. An unchanged parent is absent from the matrices and is instead injected by the immutable
digest resolved during planning.

GitHub rejects an empty dynamic matrix. Empty waves therefore contain a sentinel entry that runs a
successful no-op. If the runtime plan needs more layers than the checked-in workflow provides, the
plan fails with a regeneration command rather than silently flattening dependencies.

GitHub also limits a matrix to [256 generated
jobs](https://docs.github.com/en/actions/reference/limits#existing-system-limits). The generator
calculates a fast, safe parallel-width bound for the discovered DAG. When one wave can exceed 256
targets, it creates enough parallel shards and `github-matrix` fills each with at most 256 entries.
All shards in wave *n* depend on all shards in wave *n - 1*, preserving dependency ordering without
serialising independent images. A 600-target independent wave therefore becomes three matrices of
256, 256, and 88; a 600-target chain remains 600 one-entry waves rather than 1,800 jobs.

For pull requests, the planner calculates the Git merge base of the checked-out head and
`origin/<default-branch>`; it does not treat the moving base-branch tip as GitLab's semantically
different diff-base variable. The generated checkout fetches complete history so that comparison
is available.

## Generated jobs

The workflow contains:

- `validate`, which runs for pushes and all pull requests without registry credentials;
- `plan`, which checks out full history, calculates `image-plan.json` once, emits matrices, and
  uploads the plan as a run-scoped artifact;
- `image_layer_<n>`, or `image_layer_<n>_shard_<m>` for a wave wider than 256, a dynamic matrix in
  which each entry downloads and strictly validates the authoritative plan before building one
  exact target and uploading its immutable result;
- `manifest`, which verifies all target results against the plan and uploads
  `image-build-manifest-<commit>`; and
- `promote`, which runs only for a default-branch plan and only after manifest verification has
  succeeded.

The plan artifact contains exact output tags and exact local dependency inputs. Build jobs push
before completing, so separate runners do not need a shared container image store. Each result
records the pushed digest; the manifest maps the source commit to every built target's tag,
`@sha256` reference, and inputs. Promotion does not start until every affected image is available
and the complete result set is verified. Multi-repository registry updates are not transactional,
so use the digest-pinned manifest when a deployment needs an exact image set.

## Test, deploy, or release the newly built images

The manifest is the handoff contract. Download `image-build-manifest-<commit>`, then select a target
by logical name:

```bash
IMAGE_NAME=api
IMAGE_REF="$(jq -er --arg name "$IMAGE_NAME" \
  '.images[$name].immutable_reference' image-build-manifest.json)"
docker run --rm "$IMAGE_REF" ./smoke-test
```

A later workflow must select the successful **Container images** run whose `head_sha` equals the
commit being tested or released. Do not download an artifact from merely the latest default-branch
run. Keep `actions: read` scoped to that lookup, verify the manifest with the expected commit, and
pass the digest to the deployment system.

Once required tests pass, semantic-release, Release Please, Changesets, or another versioning step
may calculate `v1.2.3`. Authenticate the registry transport and promote the tested bytes:

```bash
platform images promote-manifest image-build-manifest.json \
  --tag "$RELEASE_TAG" \
  --expected-commit "$GITHUB_SHA" \
  --registry-transport docker
```

This command accepts only a default-branch manifest and uses each recorded `@sha256` source; it
does not rebuild. Use `--image api` to release one manifest entry, or omit `--image` to release the
whole affected set. For a coordinated version that must exist on every image, dispatch the build
workflow with `rebuild_all` first and release that full manifest. See the
[complete lifecycle](../README.md#from-commit-build-to-semantic-release).

## Required repository variables and secrets

Every provider needs the registry hostname in `PLATFORM_IMAGES_REGISTRY`, or in the custom variable
named by `registry.registry_environment_variable`. Generic OCI registries—Nexus, GitLab Container
Registry, GHCR, Harbor, and other Distribution-compatible services—default to credentials stored
as GitHub Actions secrets:

| Secret or variable | Value |
| --- | --- |
| `PLATFORM_IMAGES_REGISTRY` (variable) | Hostname such as `nexus.example.com`, without a scheme or repository path. |
| `PLATFORM_IMAGES_REGISTRY_USERNAME` (secret) | Robot, deploy-token, or workload-identity username with pull/push access. |
| `PLATFORM_IMAGES_REGISTRY_PASSWORD` (secret) | Corresponding short-lived token or password. |

The secret names follow `registry.username_environment_variable` and
`registry.password_environment_variable`; the generated workflow maps same-named GitHub secrets
only into registry-using jobs. For GHCR, supply a token with package-write permission as the
configured password; the workflow does not assume that `GITHUB_TOKEN` is itself that password.

GHCR can instead use the workflow's native identity without creating either secret:

```toml
[registry]
provider = "oci"
authentication = "credentials"
username_environment_variable = "GITHUB_ACTOR"
password_environment_variable = "GITHUB_TOKEN"
```

The GitHub renderer maps those two conventional names to `github.actor` and the job's automatic
`GITHUB_TOKEN`, and grants `packages: write` only to the registry-using jobs. The repository or
organisation must still allow Actions to write packages.

Amazon ECR instead uses GitHub Actions repository or organisation variables:

| Variable | Value |
| --- | --- |
| `PLATFORM_IMAGES_REGISTRY` | ECR hostname such as `123456789012.dkr.ecr.eu-west-2.amazonaws.com`. |
| `AWS_ROLE_TO_ASSUME` | ARN of the short-lived GitHub Actions role. |
| `AWS_REGION` | Region in which the OIDC role is assumed. Stable lookup and login still derive and verify the registry region from the ECR hostname. |

Only ECR jobs request `id-token: write` and emit the AWS credentials action. Configure its IAM
trust policy for the intended repository, branch, and pull-request subjects, and grant only the ECR
operations described in [ECR setup](ecr-setup.md).

For an already authenticated self-hosted ECR runner, omit the OIDC action:

```bash
platform images generate-workflow github \
  --runner self-hosted \
  --aws-auth ambient \
  --output .github/workflows/container-images.yml
```

`ambient` means the runner must already expose short-lived AWS credentials to AWS CLI. It does not
mean anonymous registry access.

Generic OCI providers also support `authentication = "ambient"`. In that mode the generated
workflow emits no credentials and `registry-login` is deliberately a no-op: the selected transport
must already be authenticated on that trusted runner. Stable OCI API requests must work
anonymously or runner provisioning must expose both configured credential environment variables;
the controller does not scrape a transport's credential store.

## Container backend selection

Docker is the generated default because GitHub-hosted Ubuntu runners provide Docker and Buildx:

```bash
platform images generate-workflow github \
  --builder docker \
  --registry-transport docker \
  --output .github/workflows/images.yml
```

Docker builds with `docker buildx build --push` and obtains the pushed registry digest from the
Buildx metadata file. Local Docker builds use `--load`. Named local dependencies use
`docker-image://<exact-reference>` contexts.

Generate a Podman workflow when that is the team's standard:

```bash
platform images generate-workflow github --engine podman --output .github/workflows/images.yml
```

The generated Ubuntu workflow installs Podman. Podman builds use `container-image://` contexts and
record the pushed digest through `podman push --digestfile`.

Buildah is another daemonless containers-storage builder and transport:

```bash
platform images generate-workflow github --engine buildah --output .github/workflows/images.yml
```

For a preconfigured containerd runner with BuildKit running:

```bash
platform images generate-workflow github \
  --runner self-hosted \
  --engine nerdctl \
  --output .github/workflows/images.yml
```

The generated Ubuntu workflow installs Podman and/or Buildah when selected. It verifies nerdctl,
containerd, and BuildKit but deliberately does not replace a self-hosted runner's containerd
topology. All supported pairs apply the same OCI source, revision, target, dependency-input, and
creation labels. See [container backends](container-backends.md) for versions and valid pairings.

## Pull-request trust boundary

Running an edited container build file is arbitrary code execution. The generated workflow never
uses `pull_request_target`. Pull requests from forks run only `validate`; the credentialed planning
and image jobs are skipped. Same-repository pull requests can build review images when the
configured registry trust or credential policy permits them. Review images use
`ci-<run-id>-<commit>` tags and are never promoted.

If the project must build fork contributions, use a separately reviewed, unprivileged workflow
that cannot push to the production registry. Do not expose a registry-writing role to untrusted
fork code.

## Manual complete rebuild

Run the workflow from the Actions tab with `rebuild_all` selected. This invokes `plan --ci --all`
and is useful for first adoption, a new registry namespace, or intentional cache-baseline recovery.
Normal push and pull-request runs leave it false and calculate the minimal affected graph.

## Generator options

| Option | Purpose |
| --- | --- |
| `--default-branch <name>` | Sets the push trigger and the branch whose successful plans may promote stable aliases. |
| `--runner <label>` | Sets `runs-on` for every generated job. One simple label is supported. |
| `--builder docker\|podman\|buildah\|nerdctl` | Selects the build implementation. Docker remains the generated default. |
| `--registry-transport docker\|podman\|buildah\|nerdctl` | Selects login, retry inspection, push, and promotion tooling. |
| `--engine <name>` | Backward-compatible shorthand selecting the same supported tool for both axes. |
| `--aws-auth oidc\|ambient` | ECR only: emits the pinned AWS credentials action and `id-token: write`, or relies on runner credentials. |
| `--aws-role-variable <name>` | Changes the GitHub variable containing the OIDC role ARN. |
| `--aws-region-variable <name>` | Changes the GitHub variable containing the OIDC region. |
| `--output <path>` | Writes below the repository root and creates parent directories. Omit it to print YAML to stdout. |

All third-party actions in generated YAML are pinned to full commit SHAs. Regeneration with a newer
`platform-images` release is how those pins and the installed controller version are upgraded.
