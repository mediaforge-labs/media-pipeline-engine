# Security Policy

## Public repository boundary

This repository may contain orchestration code, worker bootstrap code, packaging utilities, documentation, and encrypted runtime bundles. It must not contain proprietary plaintext core source or live credentials.

## Secrets

Use GitHub Actions secrets for runtime credentials. At minimum, the encrypted core workflow expects:

- `MEDIAFORGE_CORE_KEY_B64` — base64-encoded 32-byte AES-256 key used to decrypt `core.bundle.enc`.

Do not place secrets in workflow YAML, committed `.env` files, logs, artifacts, issue bodies, or pull-request descriptions.

## Encrypted bundle format

The current bundle format uses AES-256-GCM with a random 12-byte nonce and authenticated associated data identifying the MediaForge bundle format. Integrity/authentication failure aborts execution.

## Runtime handling

Workers decrypt the proprietary core only into an ephemeral temporary directory. Generated workflow artifacts are configured for two-day retention.

## Incident handling

If a key or credential is exposed, rotate/revoke it immediately, remove it from future commits, and treat any encrypted bundle protected only by that key as compromised until rebuilt with a new key.
