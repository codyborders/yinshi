# Yinshi macOS Desktop App Plan

## Context

Yinshi is currently a browser-first React/Vite client backed by FastAPI, with a Node.js sidecar that runs the pi SDK. The requested outcome is a macOS desktop distribution with feature parity, access to repositories on the Mac, and both local and existing cloud-backed operation/storage.

The current system already supports:

- GitHub App and allow-listed server-local repository imports.
- Isolated git worktrees, streamed agent sessions, changed-file inspection, and an interactive terminal.
- Per-user SQLite/storage boundaries and optional per-user containers.
- BYOC runner registration, heartbeat, and storage-profile validation. Workload dispatch is not implemented yet.

The desktop release must retain full current feature parity, require a Yinshi account, and support both Mac-local execution/storage and the existing BYOC cloud path (including S3 and, where supported, Archil). Local and cloud workspaces remain separate, but users must be able to move work between them. The preferred experience is continuation in either direction; a deliberate export/import workflow is the acceptable fallback if safe live handoff is not feasible.

### Confirmed product decisions

Yinshi will support three execution locations: Mac-local, hosted Yinshi, and a user-owned BYOC runner. An account is required for all three. The desktop app connects only to the official hosted service in the initial release, and its bundled runtime must be self-contained. Electron is the approved host. Existing hosted account, repository, workspace, session, and settings records will survive through additive migrations and continue to appear unchanged. Heartbeat-only runners must update their agent and repeat fingerprint pairing before they can execute workloads.

The app manages clones and isolated worktrees so it never edits a selected checkout automatically. Dirty repository imports require a review and explicit confirmation of uncommitted and untracked files. Export is a separate checked action that creates a branch ref without changing the target checkout. Workspace movement is also explicit: each move creates an immutable destination snapshot, retains the source, and supports every direction among local, hosted, and BYOC storage.

Initial sign-in is mandatory. A signed lease then permits Mac-local work without connectivity for at most 30 days, while hosted and BYOC actions remain unavailable offline. Logout and uninstall preserve encrypted per-account profiles. A separate destructive action removes profile data. Provider credentials stay at their execution location: local secrets use a Keychain-held device key, cloud secrets remain encrypted in cloud storage, and transfers contain no provider credentials.

Feature parity covers runner administration, terminals, Pi configuration, OAuth, telemetry, and settings. Local terminals and agents may use the login shell, Homebrew, the SSH agent, and installed developer tools. Yinshi itself still uses its bundled runtime. First-run and workspace-creation disclosures must explain that agents and terminals receive the logged-in macOS user's filesystem and network permissions. The app requests no unrelated macOS privacy permissions.

The shared browser client and desktop app both need the end-to-end encrypted BYOC channel, including browser use while the desktop app is closed. S3/EBS is the production BYOC profile; Archil remains visibly experimental.

Distribution defaults are a direct notarized DMG, signed automatic updates, and Apple Silicon support on the current and previous major macOS releases. Intel support can follow demonstrated demand through a universal build.

Datadog remains enabled under a deny-by-default schema. Renderer, backend, sidecar, desktop-host, runner, and crash-reporting paths must mask user input and exclude prompts, terminal data, paths, file and repository content, credentials, OAuth data, and transfer payloads. There is no reduced-MVP or delivery-date constraint; the design targets the complete product.

## Approach

Use Electron as the desktop host. It reuses the React renderer and Node sidecar, supports native folder dialogs and signed updates, and avoids adding Rust while a separate bundled Python service is still required. Package the FastAPI backend as a PyInstaller one-directory signed helper, run the pi sidecar in a supervised Node utility process, and package with Electron Builder; do not require Podman on macOS.

Electron owns Keychain access and never exposes refresh credentials or database master keys to the renderer. Give the Python helper only the active profile key and short-lived cloud access tokens over a private inherited pipe/broker, never argv, environment variables, URLs, or disk. Launch helpers with a minimal environment. Separately resolve the user's login shell environment and pass only an allowlist needed for development (`PATH`, locale, temporary directory, SSH agent, and known language-toolchain roots) to agent/terminal children; do not inherit arbitrary secret-valued environment variables. Start interactive terminals as the user's login shell. Use the bundled Git for Yinshi-managed operations while allowing user tools from the resolved PATH inside agent commands.

Serve the packaged renderer from a random loopback FastAPI port so existing relative HTTP, SSE, WebSocket, cookie, and CSRF behavior remains same-origin. Electron starts the helpers with per-launch socket paths and bootstrap secrets, waits for explicit readiness/version checks, then loads the UI. The backend accepts only loopback traffic with strict Host/Origin checks and a one-time bootstrap that creates an HttpOnly desktop session. Run Electron with context isolation, renderer sandboxing, no Node integration, a narrow typed preload bridge, navigation/window-open denial, and system-browser handling for allowlisted HTTPS links.

Add a runtime-location abstraction shared by repository, workspace, session, stream, terminal, settings, and file APIs. The renderer uses location-qualified routes and IDs (`local`, `hosted`, or a BYOC runner) rather than a process-wide API base. Keep existing hosted `/api/...` routes backward-compatible. In desktop mode, a same-origin gateway maps qualified requests to the local API, the official hosted API, or an encrypted runner RPC; it handles SSE and terminal WebSocket streaming without exposing cloud refresh tokens to the renderer.

Desktop sign-in begins at the official hosted Yinshi service in the system browser. Use a PKCE-bound, one-time desktop authorization code returned to the app's loopback callback, then keep the rotating refresh credential in macOS Keychain. Cache a signed account lease for no more than 30 days so local work survives temporary loss of connectivity; enforce logout/revocation as soon as the service is reachable. A device-local master key in Keychain wraps local database/field encryption keys. Existing GitHub App installations remain account-scoped and issue short-lived installation tokens to authenticated execution locations.

Keep ownership explicit:

- Hosted control plane: identity, device grants/leases, GitHub installations, runner registration/public key/status, and account-level feature policy.
- Each execution location: repositories, workspaces, sessions/messages/Pi JSONL, provider credentials, Pi configuration, runtime catalog, and terminals. Hosted, BYOC, and local secrets never copy implicitly between locations.
- Desktop device: Keychain material, encrypted local profiles, UI preferences, updater state, and opaque source-repository links. Store profiles beneath `~/Library/Application Support/Yinshi/profiles/<account-id>/` with owner-only permissions and no user-derived path components.
- Transfer snapshot: only the selected repository/workspace/session state; no Yinshi/provider credentials, Pi configuration, account tokens, telemetry, or source-path links. Committed Git history is transferred as repository content and is not rewritten by a secret scanner.

Expose a location selector in repository import, workspace/session navigation, and location-scoped settings. Aggregate sidebar records by `(location, id)` and label their location; keep runner administration and GitHub account management control-plane scoped. Offer a separate explicit copy action for non-secret Pi configuration when users want the same config in another location, encrypting it over the BYOC channel.

Bundle SQLCipher and require encryption for desktop control and tenant databases, deriving separate keys from the Keychain-held device key. Keep active Git repositories/worktrees as ordinary owner-only files so Git and the user's toolchain can use them; rely on FileVault for transparent volume encryption and warn when FileVault is disabled. Deleting a retained profile first destroys its Keychain wrapping material, then removes databases, repositories, runtime homes, caches, and redacted logs. Logout alone preserves them.

Build a restricted worker ASGI application from the existing repository/workspace/session/file/stream/terminal services instead of duplicating their behavior. Inject an authenticated single-user tenant context, local control/tenant stores, and the existing sidecar runtime, while excluding account, runner-administration, Datadog-proxy, and other control-plane-only routes. The desktop gateway can invoke the local worker directly; a BYOC agent carries allowlisted HTTP-like commands and multiplexed stream/terminal frames to the same worker contract over its outbound relay connection.

`backend/src/yinshi/runner_agent.py` currently implements registration and heartbeat only, so add command dispatch, event/terminal multiplexing, cancellation, resumable artifact transfer, reconnect/idempotency, capability and version negotiation, and revocation. Use the standard `Noise_IK_25519_ChaChaPoly_SHA256` handshake through maintained interoperable implementations, not a home-grown cipher protocol. The runner generates and retains its static key; each browser/desktop connection uses a one-time client key bound to user, runner, scopes, protocol version, and expiry by a short-lived control-plane-signed capability. Noise transcript binding, ordered cipher nonces, authenticated frames, replay rejection, rekeying, and hard connection limits protect the multiplexed stream. Pin the runner key after a user-confirmed fingerprint check; key changes require explicit re-pairing. Gate release on published cross-language vectors and an independent protocol review.

Encrypt command bodies, prompts, event/terminal frames, archives, provider configuration, and worker responses end-to-end between the browser/desktop client endpoint and runner. The hosted relay may observe only authorization/routing metadata, bounded ciphertext sizes, timing, and health/capability data. Implement the crypto/relay adapter in the shared frontend so browser users retain full BYOC access while the desktop is closed.

Keep local and cloud workspaces separate. Moving a workspace creates an immutable, versioned snapshot at the destination while leaving the source intact. The snapshot includes a Git bundle, working-tree/index/untracked state, relevant repository/workspace/session/message rows, and the durable Pi JSONL context under the workspace runtime home. It excludes application/provider credentials, absolute source paths, sockets, logs, ignored/untracked secret files, and machine-specific configuration; committed Git content is preserved without history rewriting. Require the source to be quiescent, verify hashes and archive limits before extraction, rewrite destination paths/IDs transactionally, and resume exact context only when `pi_context_version` and sidecar format versions are compatible. Otherwise import the code and visible transcript but create a fresh agent session with a clear warning. There is no background bidirectional sync.

Native folder selection returns a short-lived opaque capability, not an arbitrary path accepted from renderer JavaScript. Electron sends the canonical path and filesystem identity to the local helper over its privileged bootstrap channel; the UI receives only a display label and selection token. Import is a two-phase inspect/confirm operation that rechecks HEAD and status at confirmation, shows included dirty/untracked files plus ignored/secret exclusions, and then clones and overlays only the approved state. Export uses the same capability mechanism, refuses a dirty or mismatched target and branch collisions, and pushes a new branch ref without changing the target worktree.

Make telemetry schema-based and deny-by-default. Continue disabling replay and interaction capture, normalize every route before emission, and use Datadog `beforeSend` filters to drop free-form error messages, request bodies, query strings, resource URLs, view names, and any event that cannot be proven metadata-only. Emit only enumerated operation names, coarse duration/size buckets, app/runtime versions, macOS/architecture, location type, status/error codes, and random rotating installation/session identifiers. Apply the same redactor before Electron crash metadata and all backend/sidecar/runner logs; never upload prompts, terminal bytes, file/repository names or paths, source/tool output, imported configuration, provider/model labels entered by users, email/account names, OAuth values, secrets, or encrypted payloads. Do not upload native minidumps or buffered console output because they cannot be reliably scrubbed; report allowlisted crash type/build/process metadata only. Current code already disables replay and masks DOM fields in `frontend/src/rum.ts`, but raw exception/path logging across Python and Node requires an audit and replacement.

## Files to modify

Work spans five areas. The new `desktop/` package owns Electron main/preload code, process and credential brokers, native dialogs, telemetry, updates, Builder configuration, entitlements, and tests. Frontend changes cover `package*.json`, `src/App.tsx`, `main.tsx`, `api/client.ts`, relevant hooks and components, `rum.ts`, its tests, and new runtime-location and encrypted-runner modules.

Backend application-mode and identity work belongs in `backend/src/yinshi/{main,config,auth,db,tenant,models}.py` and `api/deps.py`. Route changes cover auth, repositories, workspaces, sessions, streams, terminals, workspace files, runners, and settings. Existing crypto, provider, Git, workspace, file, sidecar-runtime, and runner services will gain interfaces; new worker, gateway, device, relay, transfer, and telemetry services will hold new responsibilities.

The sidecar changes include its package manifests, `src/{sidecar,constants,git_auth}.js`, and tests for environment handling, readiness contracts, portable context, and safe logging. Security, deployment, desktop installation, privacy, and recovery documents also change, including the AWS runner template. CI gains macOS helper/toolchain builds, Electron tests, SBOM and license generation, signing, notarization, checksums, and update metadata.

## Reuse

The React routes and workbench UI remain the presentation foundation, while `frontend/src/api/client.ts` supplies established HTTP, SSE, and WebSocket behavior. Backend repository validation, cloning, workspace and worktree lifecycle, request-scoped tenant access, FastAPI services, tenant SQLite model, and envelope encryption remain authoritative. New location interfaces must wrap these components so desktop behavior does not fork domain logic.

Durable Pi JSONL handling in `sidecar_runtime.py` and `sidecar.js` supports compatible context continuation. The existing pi SDK and terminal bridge remain in the sidecar. The secret-path and symlink policy in `workspace_files.py` becomes the common import, transfer, and export policy. Portable snapshots can adapt the SQLite snapshot, streaming AES-GCM, manifest, and private-staging patterns in `backup.py` without copying whole-server backups.

Cloud-runner registration, storage validation, backend types, and UI are extended from their current liveness-only state. Existing loopback binding, trusted hosts, CORS, cookies, CSRF, and transport controls receive explicit desktop-origin rules. Privacy work builds on the current RUM settings—replay disabled, interaction capture disabled, and DOM masking—then adds allowlisted events and cross-process redaction.

## Steps

For every behavior-bearing item below, first add the named public-seam test, run it to observe a clean relevant failure, implement only that slice, then run it green before refactoring or starting the next slice.

### 1. Privacy baseline

- [ ] Define a small versioned telemetry schema and safe logger APIs in TypeScript, Python, and sidecar JavaScript. Canary tests cover `rum.beforeSend`, process logs, offline queues, and crash metadata. Remove raw exceptions, URLs, paths, labels, emails, prompts, and console payloads from current call sites. Unknown fields are rejected; replay, interaction capture, console forwarding, and minidump upload stay disabled.

### 2. Application modes and storage

- [x] Test an app factory that produces backward-compatible `hosted`, restricted `worker`, and `desktop` applications with explicit route allowlists. Add schema-version migrations, SQLCipher desktop control and tenant databases, owner-only profile/runtime directories, Keychain-key injection, FileVault reporting, and fail-closed startup when encryption or helper versions are unavailable.

### 3. Secure Electron shell

- [x] Test single-instance behavior, inherited-pipe bootstrap, random loopback binding, readiness negotiation, crash-loop handling, process-tree shutdown, and restart recovery against fake helpers. Add hardened BrowserWindow and preload policy, same-origin SPA serving with CSP, an HttpOnly bootstrap session, blocked navigation and popups, allowlisted external links, and no renderer access to Node, Keychain, arbitrary paths, or helper secrets.

### 4. Desktop account grant

- [x] Cover PKCE authorization, loopback-target validation, one-time codes, refresh rotation and reuse detection, device listing and revocation, signed 30-day leases, expiry, account switching, and offline logout with API tests. Implement the system-browser flow and Electron Keychain broker. Logout preserves local profiles; only the confirmed removal action destroys their wrapping material.

### 5. Location-aware client and gateway

- [x] Test `RuntimeRef` and `RuntimeTransport` for JSON, upload, SSE, and terminal WebSocket calls. Add location-qualified routes and IDs, legacy hosted redirects, aggregate sidebar states, import and settings selectors, and gateway routing for local and official-hosted APIs. Existing browser URLs and hosted cookie behavior remain compatible.

### 6. Mac-local runtime parity

- [x] Exercise GitHub and local imports, workspaces, session history, prompt streaming and cancellation, model catalogs, file operations, terminals, provider authentication, Pi settings and release notes, deletion, and relaunch persistence with `container_enabled=false`. Run the contract first against the mock sidecar and then against the packaged sidecar. Remove container-only assumptions, use private launch sockets and bundled Git, and restrict child environments to approved shell and toolchain fields.

### 7. Native import and branch export

- [ ] Test one-use selection capabilities, expiry, path or inode replacement, symlink traversal, status races, large and binary files, Git status classes, `.env*` and ignored exclusions, cancellation, and source immutability. Implement inspect/confirm cloning with the approved dirty overlay. Export creates a new ref in a reselected clean matching repository and refuses collisions or concurrent changes without changing its checkout.

### 8. Encrypted BYOC foundation

- [x] Publish the protocol and cross-language Noise vectors. Cover key registration and fingerprint confirmation, capability scope and expiry, handshake identity, malformed and replayed frames, nonce limits, fresh handshakes, reconnect, revocation, queue and frame limits, and ciphertext-only relay persistence. Implement the outbound runner WebSocket, opaque relay, browser/desktop Noise transport, version negotiation, and explicit re-pairing for old or changed keys.

### 9. Restricted worker slices

- [x] Build through the encrypted public transport in this order: health and repository operations; workspace and session CRUD; prompt events with a durable journal, sequence reconnect, and idempotent cancellation; terminal multiplexing; workspace files; location-scoped provider setup; and Pi configuration, catalogs, and commands. Account, runner administration, telemetry proxy, and raw filesystem routes stay absent. The same contract suite runs against hosted, local, and BYOC implementations.

### 10. Portable workspace transfer

- [ ] Start with a versioned manifest and chunk-authenticated resumable archive. Quiesce active runs and terminals. Capture Git refs, index and working-tree state, approved untracked files, initialized submodules and LFS objects, selected database rows, and Pi JSONL without following overlay symlinks. Enforce entry, total, count, and compression limits plus hashes, normalized paths, private staging, transactional ID/path rewrites, idempotency, expiry, and cleanup. Test every directed location pair, interruption, source retention, naming collisions, compatible continuation, and incompatible fresh-context fallback.

### 11. Profiles, offline behavior, and recovery

- [ ] Test the 30-day offline window, expiry lockout without data loss, reconnect and revocation, retained accounts, key loss, corrupt databases, stale locks and sockets, orphan helpers, incomplete imports, and profile removal. Add bounded encrypted metadata-only telemetry buffering plus backup and export documentation. User content never enters the telemetry queue.

### 12. Signed distribution and updates

- [ ] Add macOS CI coverage before packaging the PyInstaller helper, Electron and sidecar native modules, pinned Git, frontend assets, licenses, and SBOM material. Verify helper hashes and versions before launch. Produce signed and stapled arm64 DMG/update artifacts, defer installation during active work, exercise N-1 and interrupted upgrades, and publish through a staged HTTPS feed with rollback controls. Archil remains experimental.

### 13. Documentation and release gates

- [ ] Update threat models, privacy guarantees, full-user-permission disclosure, FileVault guidance, data locations, runner pairing, S3/EBS layout, Archil status, movement semantics, provider reconnection, profile deletion, diagnostics, recovery, and release procedures. Independent crypto and desktop security reviews are required before release.

## Verification

### Automated

- `cd backend && pytest -q && black --check src tests && isort --check-only src tests && flake8 src tests && mypy src`
- `cd frontend && npm exec vitest run && npm run typecheck && npm run build`
- `cd sidecar && node --test tests/*.test.js && npm audit`
- Run the new desktop unit/type tests and Playwright Electron suite on signed-package-like helpers, plus existing browser Playwright tests against hosted and BYOC transports.
- Run one shared runtime contract suite against local, hosted, and encrypted BYOC; run transfer fixtures across all six directions and include dirty worktrees, Unicode/long paths, binary files, LFS, submodules, reconnects, and malicious archives.
- Inject unique canaries as prompt text, terminal input/output, paths, file content, repo names, email, provider labels, OAuth values, and secrets. Assert they and common encodings never appear in Datadog payloads, local/uploaded logs, relay records, update/crash reports, or metadata queues; separately confirm BYOC relay captures cannot reveal request/response plaintext.

### macOS and release

- On every supported macOS release, exercise clean install, first login, all three locations, offline grace/expiry, account switching, local import/export, every workspace feature, each transfer direction, relaunch after forced termination, runner loss/re-pair, update deferral, N-1 upgrade, and uninstall/reinstall with retained data.
- Attack the loopback service from another browser/local process; test Host/Origin/CSRF/bootstrap rejection, OAuth state/code replay, refresh-token reuse, worker route escapes, Noise replay/tamper, path/symlink races, archive bombs, stale capabilities, and malicious renderer navigation/IPC.
- Verify no Podman/Node/Python/Git installation is required for Yinshi itself while user Homebrew/language tools and SSH agent remain available in the disclosed full-permission mode.
- Verify release artifacts with `codesign --verify --deep --strict`, Gatekeeper assessment, notarization/stapler validation, mounted-DMG launch, nested Mach-O signatures, updater signature rejection, checksums, dependency audits, and generated SBOM/license notices.
