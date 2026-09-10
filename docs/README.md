# Modelfiche documentation

This directory is the documentation hub for operators, automation authors, and
contributors. Start with the guide matching the task; design plans are not
runtime instructions.

## Operator guides

- [Installation and operations](OPERATIONS.md) — app setup, remote access,
  storage, credentials, backups, restore, and release builds.

- [CLI guide](CLI.md) — install `mfiche`, discover commands, run common
  workflows, interpret output, and recover from errors.
- [Agent guide](AGENT_GUIDE.md) — deterministic JSON-first automation contract,
  safety rules, and copy-ready recipes.
- [W&B-compatible trainer logging](WANDB_COMPATIBILITY.md) — AI Toolkit and
  kef-krea2 signed environments, outbound checkpoint handoff, optional live
  telemetry, and protocol verification.
- [FAL endpoint schemas](../FAL_ENDPOINT_SCHEMAS.md) — supported generation
  adapters and endpoint-specific request fields.

## Product and implementation reference

- [Product definition](../PRODUCT.md) — users, domain language, design
  principles, and product boundaries.
- [CLI contract](../CLI_SPEC.md) — stable automation, safety, output, and
  compatibility requirements.

## Contributor guides

- [Contributing](../CONTRIBUTING.md) — setup, focused tests, migrations, and
  pull-request expectations.
- [Security](../SECURITY.md) — supported deployment boundary, secrets, and
  vulnerability reporting.

## Documentation rules

When behavior changes:

1. Update the user-facing guide and in-app Docs screen in the same change.
2. Verify every command with `mfiche <group> <command> --help`.
3. Mark optional services and billable provider actions explicitly.
4. Keep secrets, private paths, signed tokens, and presigned URLs out of examples.
5. Prefer stable concepts and workflow examples over screenshots that age
   immediately.
