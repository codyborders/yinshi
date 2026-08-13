# Sprite Storage Encryption Assessment

Assessment date: 2026-08-13

## Decision

Launch stays blocked. Current Fly Sprites documentation does not provide an encryption-at-rest field bound to a Sprite or storage identity. Without that provider fact, Yinshi cannot verify the security property for each managed filesystem during creation, recovery, reconciliation, or incident response.

The API returns identity, name, status, organization, URL settings, and timestamps. It does not return storage identity or encryption state. Yinshi must not infer storage encryption from guest files or runtime status.

## Reviewed material

The review covered the [Sprites API](https://sprites.dev/api/sprites), [Sprites overview](https://docs.sprites.dev/), and [lifecycle documentation](https://docs.sprites.dev/concepts/lifecycle/). It also covered [working guidance](https://docs.sprites.dev/working-with-sprites/) and [Fly security](https://fly.io/security/).

The API documents `GET /v1/sprites`, pagination, filtering, and `created_at` on full records. Lifecycle documentation describes a persistent ext4 filesystem. None of these sources state whether storage encryption can be disabled. They also expose no storage identifier or per-Sprite encryption status.

## Provider confirmation

The project owner confirmed on 2026-08-13 that Fly Sprite filesystems are encrypted at rest. This confirmation permits the isolated staging drill to create the fail-closed guest marker before installation. It does not enable public managed launch.

Launch review still needs the protected provider record. It must confirm whether every Sprite filesystem is encrypted at rest. It must identify any customer, organization, API, or support control that can disable encryption.

The statement must identify the cryptographic boundary and its immutable storage identifier. It must explain how operators can audit encryption after creation and during reconciliation. It must also describe key and block handling after Sprite deletion.

Store the response in the protected launch record. Record its reviewer and approval date. Keep both public launch controls disabled until the security owner accepts it.

## Alternative

If Fly cannot provide an auditable guarantee, use inspectable encrypted volumes or guest-managed filesystem encryption. Reassess backup and restore procedures. Reassess deletion and key recovery before selecting either option.
