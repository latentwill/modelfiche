# Contributing to Modelfiche

## Development setup

Requirements: Windows, macOS, or Linux, Python 3.11+, `uv`, Node.js, and `pnpm`.

```bash
uv sync --extra dev --extra compat-wandb
pnpm --dir apps/web install
uv run alembic upgrade head
```

Run the local stack with `uv run mfiche start`; use `uv run mfiche status` and
`uv run mfiche stop` for lifecycle checks. An editable global command is
optional:

```bash
uv tool install --editable .
```

## Change principles

- Keep validation and domain mutations in the API/service layer. UI and CLI
  clients must not write directly to SQLite or asset directories.
- Preserve typed lineage and immutable published versions.
- Treat provider submission as a distinct, consequence-aware action.
- Keep secrets in environment variables or the configured secret manager;
  persist only nonsecret references and fingerprints.
- Update every affected caller during a contract change. Do not leave aliases or
  compatibility shims unless a released external contract requires one.
- Reuse existing UI, API, schema, and test patterns rather than introducing a
  parallel convention.

## Verification

Run focused tests while developing, then the affected suite before opening a
pull request.

```bash
# Entire Python suite
uv run --extra dev --extra compat-wandb python -m pytest -q

# Frontend behavior, types, and production build
pnpm --dir apps/web test
pnpm --dir apps/web typecheck
pnpm --dir apps/web build

# Migrations
uv run alembic current
uv run alembic heads
uv run alembic upgrade head
```

Behavioral changes need proof at the changed boundary: reproduce a bug, exercise
a feature, or drive a UI route in a browser. Tests should defend observable
contracts, not source text or implementation details.

Use `python -m pytest` through `uv`, not a globally installed `pytest` executable.
The compatibility extra installs exactly `wandb==0.28.0`. Without it, the local
`wandb/` run-output directory can be imported as an empty namespace package,
which is not a working SDK. The compatibility suite runs against disposable
loopback ingress and temporary storage; it does not need a wandb.ai account.

## Database migrations

- Add an Alembic revision for every persistent schema change.
- Preserve deterministic upgrades from all supported historical baselines.
- Update migration and frozen-baseline tests.
- Never rewrite an applied migration.

## CLI changes

- Keep `--json` stdout machine-readable; diagnostics and progress belong on
  stderr.
- Preserve stable exit-code categories and structured error fields.
- Update [docs/CLI.md](docs/CLI.md), [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md),
  and [CLI_SPEC.md](CLI_SPEC.md) when the public command contract changes.
- Add or update CLI contract tests for observable command behavior.

## Frontend changes

- Preserve keyboard operation, visible focus, reduced-motion behavior, and
  mobile layouts.
- Use the shared components in `apps/web/src/ui.tsx` and established Vela theme
  conventions.
- Verify the changed route in a browser; snapshots alone are insufficient for
  layout or interaction work.

## Pull requests

Include:

- the problem and chosen behavior;
- affected contracts and migration impact;
- exact verification commands and observed results;
- screenshots only when visual comparison adds useful evidence;
- explicit notes for billable provider calls, destructive operations, or
  compatibility changes.

Do not commit credentials, generated runtime databases, provider responses,
local logs, release artifacts, or private training data.

## Windows distribution

See [docs/WINDOWS.md](docs/WINDOWS.md) for the native build and extracted-package
smoke test. Tagged releases build on Windows and publish only after validation.
The descriptor-relative LocalRoot contract tests require POSIX; Windows tests
exercise the portable browser import, cache, credentials, leases, and backups.
