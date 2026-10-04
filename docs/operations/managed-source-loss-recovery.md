# Managed Source-Loss Recovery Drill

## Current status

The scheduled workflow validates protected staging settings and exits with failure. It does not claim a live recovery result.

Live staging integration remains pending. Public launch must stay disabled until repeated live runs pass.

## Staging requirements

Use an isolated staging tenant, private Fly Sprites, and an independent versioned bucket. Configure the `managed-recovery-staging` GitHub environment with every secret named in `.github/workflows/managed-source-loss-recovery.yml`.

Do not use production tenants or Sprite checkpoints. Keep workflow permissions at `contents: read`.

## Required drill boundary

The live boundary must create representative SQLite rows and files. Include nested directories, an empty file, binary content, and a saved conversation. Record expected digests and row counts without tenant paths.

Create an encrypted managed backup. Inject a lost multipart-completion response, then retry reconciliation. Require one accepted immutable object version and no remaining multipart upload.

Delete the source Sprite through the provider API. Restore into the deterministic replacement. Restart the control plane at these transitions:

1. Candidate created before catalog publication.
2. Guest result published before activation.
3. Activation committed before final completion.

Require SQLite `PRAGMA integrity_check` to return `ok`. Compare row counts, file digests, and saved sessions. Require a new runner identity and exactly one active authority.

Exercise retention and exact-version deletion. Run Sprite reconciliation. Remove the source, failed candidates, retired sources, object versions, multipart uploads, and temporary tenant.

## Sanitized output

The retained JSON artifact may contain commit SHA, UTC start time, status, counts, and Boolean checks. It must not contain tenant paths, Sprite names, tokens, keys, bucket keys, provider bodies, or plaintext archive data.

Keep artifacts for 14 days. A missing cleanup result must fail the workflow.

## Approval

Gate 4 requires repeated passing live runs. Record the staging owner, monitoring owner, and approval date before public launch review.
