# GitHub Actions

Generate a complete workflow from the validated image graph:

```bash
platform images generate-workflow github \
  --default-branch main \
  --engine docker \
  --output .github/workflows/container-images.yml
```

Commit the generated file. Run the command again whenever a `Dockerfile` or `Containerfile` changes
the depth of the local dependency graph, when the default branch or runner changes, or when moving
between Docker and Podman. The generated header records that contract.

## Why the workflow uses layers

GitHub Actions can create a dynamic matrix from an earlier job's output, but a job's `needs`
relationships are part of the workflow definition before the run starts. A single flat matrix
would allow a consumer to race its newly rebuilt parent. The generated workflow therefore contains
one static job per maximum graph depth:

```text
validate -> plan -> image_layer_0 -> image_layer_1 -> ... -> promote
```

At runtime, `platform images github-matrix` groups only the affected plan into build-safe waves.
All images within a wave have no dependency on one another and run in parallel. The next wave waits
for the previous wave, so every selected parent has pushed its exact output before a consumer
starts. An unchanged parent is absent from the matrices and is instead injected by the immutable
digest resolved during planning.

GitHub rejects an empty dynamic matrix. Empty waves therefore contain a sentinel entry that runs a
successful no-op. If the runtime plan needs more layers than the checked-in workflow provides, the
plan fails with a regeneration command rather than silently flattening dependencies.

For pull requests, the planner calculates the Git merge base of the checked-out head and
`origin/<default-branch>`; it does not treat the moving base-branch tip as GitLab's semantically
different diff-base variable. The generated checkout fetches complete history so that comparison
is available.

## Generated jobs

The workflow contains:

- `validate`, which runs for pushes and all pull requests without registry credentials;
- `plan`, which checks out full history, calculates `image-plan.json` once, emits matrices, and
  uploads the plan as a run-scoped artifact;
- `image_layer_<n>`, a dynamic matrix in which each entry downloads and strictly validates the
  authoritative plan before building one exact target; and
- `promote`, which runs only for a default-branch plan and only after the last dependency layer has
  succeeded.

The plan artifact contains exact output tags and exact local dependency inputs. Build jobs push
before completing, so separate runners do not need a shared Docker or Podman image store. Promotion
is graph-wide: no affected `main` alias changes until every affected image is available.

## Required repository variables

The default AWS OIDC workflow expects GitHub Actions repository or organisation variables:

| Variable | Value |
| --- | --- |
| `PLATFORM_IMAGES_REGISTRY` | ECR hostname such as `123456789012.dkr.ecr.eu-west-2.amazonaws.com`. Use the custom name from `registry.registry_environment_variable` when configured. |
| `AWS_ROLE_TO_ASSUME` | ARN of the short-lived GitHub Actions role. |
| `AWS_REGION` | Region in which the OIDC role is assumed. Stable lookup and login still derive and verify the registry region from the ECR hostname. |

The workflow requests only `contents: read` globally. Jobs that need AWS OIDC additionally request
`id-token: write`. Configure the IAM trust policy for the intended repository, branch, and pull
request subjects, and grant only the ECR operations described in [ECR setup](ecr-setup.md).

For an already authenticated self-hosted runner, omit the OIDC action:

```bash
platform images generate-workflow github \
  --runner self-hosted \
  --aws-auth ambient \
  --output .github/workflows/container-images.yml
```

`ambient` means the runner must already expose short-lived AWS credentials to AWS CLI. It does not
mean anonymous registry access.

## Docker or Podman

Docker is the generated default because GitHub-hosted Ubuntu runners provide Docker and Buildx:

```bash
platform images generate-workflow github --engine docker --output .github/workflows/images.yml
```

Docker builds with `docker buildx build --push` and obtains the pushed registry digest from the
Buildx metadata file. Local Docker builds use `--load`. Named local dependencies use
`docker-image://<exact-reference>` contexts.

Generate a Podman workflow when that is the team's standard:

```bash
platform images generate-workflow github --engine podman --output .github/workflows/images.yml
```

The generated Ubuntu workflow installs Podman. Podman builds use `container-image://` contexts and
record the pushed digest through `podman push --digestfile`. Both engines apply the same OCI source,
revision, target, dependency-input, and creation labels.

## Pull-request trust boundary

Running an edited container build file is arbitrary code execution. The generated workflow never
uses `pull_request_target`. Pull requests from forks run only `validate`; the credentialed planning
and image jobs are skipped. Same-repository pull requests can build review images when the AWS OIDC
trust policy permits them. Review images use `ci-<run-id>-<commit>` tags and are never promoted.

If the project must build fork contributions, use a separately reviewed, unprivileged workflow
that cannot push to the production registry. Do not expose a registry-writing role to untrusted
fork code.

## Manual complete rebuild

Run the workflow from the Actions tab with `rebuild_all` selected. This invokes `plan --ci --all`
and is useful for first adoption, a new ECR namespace, or intentional cache-baseline recovery.
Normal push and pull-request runs leave it false and calculate the minimal affected graph.

## Generator options

| Option | Purpose |
| --- | --- |
| `--default-branch <name>` | Sets the push trigger and the branch whose successful plans may promote stable aliases. |
| `--runner <label>` | Sets `runs-on` for every generated job. One simple label is supported. |
| `--engine docker\|podman` | Selects the build, login, retry, and promotion executable. |
| `--aws-auth oidc\|ambient` | Emits the pinned AWS credentials action and `id-token: write`, or relies on runner credentials. |
| `--aws-role-variable <name>` | Changes the GitHub variable containing the OIDC role ARN. |
| `--aws-region-variable <name>` | Changes the GitHub variable containing the OIDC region. |
| `--output <path>` | Writes below the repository root and creates parent directories. Omit it to print YAML to stdout. |

All third-party actions in generated YAML are pinned to full commit SHAs. Regeneration with a newer
`platform-images` release is how those pins and the installed controller version are upgraded.
