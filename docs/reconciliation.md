# Configuration reconciliation

`platform init` solves first adoption. `platform reconcile` keeps that configuration useful as
targets and Dockerfile or Containerfile references change later:

```bash
platform reconcile
```

The command inspects only the configured discovery roots, merges unique high-confidence additions
into `platform-images.toml`, prints the exact unified diff, and validates the resulting graph. Run
it after adding or renaming an image, adopting a new registry spelling, or introducing a new short
external base.

## What it changes

Reconciliation is additive and follows the existing policy from broadest to narrowest:

| Evidence | Safe action |
| --- | --- |
| A new unqualified external base does not resemble a local target | Add it to `dockerfile.allowed_short_external_images`. |
| A qualified near-match belongs to `registry.namespace`, and the target has no output override | Add the exceptional `images.<target>.repository`. |
| A qualified near-match belongs to another namespace | Add one dependency alias. |
| The target already has a different output repository | Preserve it and add the new spelling as an input-only alias. |
| The spelling is already a repository, alias, or `identity.external_repositories` exception | Make no change. |
| Separator normalization is ambiguous, or similarity candidates are too close | Make no change and leave one grouped validation warning for review. |

The command never changes `registry.namespace`, removes a manual setting, redirects an existing
output repository, or adds discovery roots. Namespace and root changes alter ownership or publish
destinations and therefore remain explicit team decisions. Exact remote basenames continue to
resolve without configuration, regardless of registry hostname or intermediate path.

Configured `[dockerfile.arguments]` values participate in reconciliation, so a later expression
such as `${SOURCE_REGISTRY}/ubuntu-24-04-base:latest` is evaluated using the same checked-in value
used by graphing and real builds.

## Safety and review

Writes use an atomic same-directory replacement and preserve the configuration file's permissions
and LF or CRLF newline style. Unrelated settings and comments remain byte-for-byte intact. A
managed array may be rendered in the project's canonical multiline form; comments attached to the
updated setting are retained.

The proposed configuration is loaded and the complete repository graph is validated after the
write. If an inferred identity introduces a new error—for example, it closes a dependency
cycle—the original file is restored and the command fails. Existing validation errors do not
permit the reconciler to introduce additional ones.

The normal team workflow is:

```bash
platform reconcile
git diff -- platform-images.toml
platform images validate
```

The printed inference audit records the source repository, selected logical target, confidence,
reference count, and whether the update is a canonical output exception or dependency alias.

## CI drift check

Use check mode when configuration drift should fail a pull request without modifying the checkout:

```bash
platform reconcile --check
```

It prints the same proposed diff and exits with status 1 when safe updates are available. It exits
successfully when the file is already reconciled. Ambiguous references remain validation warnings,
not automatic changes, so teams retain the final decision where repository evidence genuinely
conflicts.
