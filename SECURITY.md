# Security policy

## Supported deployment boundary

Modelfiche is a single-user operator application. The default API and web UI
remain loopback-only. Local profiles provide attribution and are **not**
authentication or authorization.

Optional remote access preserves the loopback listener and places an explicit
shared-secret boundary at an HTTPS tunnel. It requires an exact remote
Host/Origin, Cloudflare tunnel metadata, and either the configured service
credential or the short-lived HTTP-only session issued from that credential.
The credential has full operator authority; keep it in a password manager and
issue it only to the single operator and their automation.

Do not expose the regular operator API directly to an untrusted network or
enable a tunnel without `TITLES_REMOTE_ACCESS_ORIGIN`,
`TITLES_REMOTE_ACCESS_CLIENT_ID`, and
`TITLES_REMOTE_ACCESS_CLIENT_SECRET`. Multi-user deployment still requires a
separate authentication and authorization design.

The optional W&B-compatible ingress is a separate, narrow protocol surface. It
must use HTTPS, preserve the documented authentication headers, and share only
the required database/storage access. See
[docs/WANDB_COMPATIBILITY.md](docs/WANDB_COMPATIBILITY.md).

## Secrets

Modelfiche stores nonsecret provider configuration and credential references.
Keep these values outside the repository and database:

- S3 access keys and session tokens;
- FAL and LLM API keys;
- Ed25519 training-signing private keys;
- run-scoped signed tokens;
- presigned URLs and provider callback credentials;
- remote-access client IDs, client secrets, and session cookies.

Use environment variables, 1Password, or an equivalent local secret manager.
Generated support bundles redact credential-shaped values and local home paths,
but review a bundle before sharing it.

## Local data

Runtime databases, assets, caches, exports, logs, backups, and trainer packets
may contain private project data. Restrict filesystem access, use encrypted
transport when copying trainer packets, and verify `checksums.json` before use.
A restore replaces the complete application data directory and therefore
requires an explicit confirmation phrase.

## Provider actions

FAL generation and some external LLM actions can incur cost. Modelfiche
separates configuration checks and previews from provider submission. Automation
must not treat a successful preview as permission to submit billable work.

## Reporting a vulnerability

Do not open a public issue containing exploit details, secrets, private data, or
presigned URLs. Report the issue privately to the repository owner through
GitHub's private vulnerability reporting facility when available. Include:

- affected version or commit;
- deployment topology;
- minimal reproduction;
- impact and required preconditions;
- whether any credential or private data may have been exposed.

Rotate affected credentials immediately. Do not attach unredacted databases,
logs, backups, or trainer packets.
