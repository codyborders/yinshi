# Middle-Ground Data Protection Threat Model

Yinshi's production target is practical tenant data protection. The service should keep stored user data encrypted, keep tenant stores separated, reduce accidental operator exposure, and use TLS for browser traffic. This model does not make Yinshi a zero-knowledge host. The server still runs the agent, so it must handle plaintext during active work.

## Security Target

The intended protection is against copied disks, backup leaks, VM snapshots, and direct inspection of database files outside the running service. It also covers ordinary tenant breakout risks: one user's request should not reach another user's database, workspace, Pi config, sidecar state, or runtime socket. Network observers should not see browser sessions or OAuth/model-provider traffic in plaintext.

This model deliberately does not claim protection from a root administrator, hypervisor operator, or attacker with live process access. During a session, the backend and sidecar decrypt repositories, prompts, imported settings, model credentials, and related runtime files to perform the requested work. SQLCipher, encrypted filesystems, envelope keys, and containers reduce stored-data exposure and narrow runtime access. They do not stop a privileged host operator from inspecting memory, mounted workspaces, process state, or Unix sockets while work is running.

Strict admin-denial would require a different design. Plaintext execution would need to move to a user-trusted runner, a user-owned VM, or a confidential VM that releases keys only after attestation. That is outside this middle-ground scope.

## Controls in Scope

Per-user data encryption keys are wrapped with `KEY_ENCRYPTION_KEY` and tagged with `KEY_ENCRYPTION_KEY_ID`, which gives deployments a clean rotation handle. Existing `ENCRYPTION_PEPPER` wrapped keys remain readable so deployments can migrate without losing accounts.

Tenant SQLite databases can use SQLCipher when `TENANT_DB_ENCRYPTION` is enabled. In required mode, startup and tenant DB access fail closed if no supported SQLCipher DB-API module is installed. Imported Pi settings in the control database can use field-level AES-256-GCM through `CONTROL_FIELD_ENCRYPTION`.

Sidecar containers use `CONTAINER_MOUNT_MODE=narrow` by default. The container receives only the active workspace, repository checkout, read-only Pi config path, and workspace-scoped Pi session file needed for the current request. The tenant database and tenant data root are left outside the sidecar mount set.

Durable Pi session files are stored as JSONL under the user's workspace runtime home. They contain prompts, assistant output, tool calls, tool results, file snippets, terminal-adjacent command output, and compaction summaries. Treat these files as sensitive user data with the same protection level as transcripts and workspace files. SQLCipher protects tenant SQLite databases, not these JSONL files, so production deployments that need stored context protection must place `USER_DATA_DIR` on encrypted storage.

Production transport hardening is controlled by `REQUIRE_HTTPS` and `HSTS_ENABLED`. With HTTPS required, plaintext requests are redirected to HTTPS and secure session cookies are enabled. The reverse proxy must still terminate TLS and forward `X-Forwarded-Proto` correctly.

## Operator Duties

Keep `KEY_ENCRYPTION_KEY`, OAuth secrets, GitHub App private keys, and deployment credentials outside source control. Put them in `.env` or the VM's secret manager. Before using `TENANT_DB_ENCRYPTION=required`, install and test `sqlcipher3` or `pysqlcipher3` on the VM.

Place `USER_DATA_DIR` on storage that is encrypted below the application layer, such as fscrypt, LUKS, or an encrypted VM disk. When `USER_DATA_ENCRYPTION=required`, add a `.yinshi-encrypted-storage` marker at the encrypted mount root after provisioning. The marker is only a fail-closed check; it does not create encryption by itself.

GitHub App installation tokens should stay short-lived and in memory. Do not log provider secrets, GitHub tokens, decrypted settings, or prompt payloads. Keep production `FRONTEND_URL` on HTTPS and remove localhost origins outside debug mode.

## Residual Risk

A privileged host operator can still inspect live memory, container filesystems, bind-mounted paths, or the sidecar socket during an active session. A compromised backend can request user DEKs, SQLCipher keys, and fresh GitHub installation tokens because the server remains trusted in this model.

Some routing metadata remains plaintext so the control plane can authenticate users and route requests. That includes user ids, email addresses, provider identifiers, and timestamps. Field-level encryption covers sensitive payloads. Operational metadata stays available to the control plane when routing requires it.
