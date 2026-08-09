# BYOC Encrypted Runner Protocol

Status: version 1, health RPC implemented; remaining worker scopes are not release-ready.

## Security goal

The protocol carries worker requests and responses between a Yinshi browser or desktop renderer and a user-owned runner. The hosted control plane authorizes and routes a connection, but it must not receive command bodies, prompts, terminal bytes, source files, workspace archives, or worker responses in plaintext.

The relay can observe:

- Random runner and transfer identifiers.
- Capability metadata issued by the control plane.
- Ciphertext sizes, direction, timing, and connection lifetime.
- Runner connection and health state.

The relay can delay, drop, reorder, replay, or replace frames. Noise authentication and ordered nonces cause altered, replayed, or out-of-order frames to fail. The relay can always deny service; this protocol does not attempt to hide traffic shape or provide availability against the relay.

## Protocol identifiers

| Field | Value |
|---|---|
| Application protocol | `yinshi-runner-v1` |
| Noise protocol | `Noise_IK_25519_ChaChaPoly_SHA256` |
| Capability type | `YINSHI-RUNNER-CAP` |
| Capability lifetime | 300 seconds |
| Noise frame maximum | 65,535 bytes |
| Re-handshake threshold | 1,048,576 messages per direction |
| Relay queue limit | 16 frames per transfer |
| Concurrent transfers per runner connection | 32 |

All JSON sent inside the Noise channel is UTF-8. Protocol-generated JSON uses sorted keys and compact separators. Receivers validate exact field sets, types, versions, UUIDs, sequence numbers, and size limits before dispatch.

## Keys and pairing

A runner creates one X25519 static private key on first registration. It stores the raw 32-byte key in an owner-only regular file and advertises the canonical, unpadded base64url public key during the one-time registration exchange. Heartbeats cannot change this key.

The settings page displays the complete SHA-256 fingerprint of the raw public key:

```text
SHA256:<64 lowercase hexadecimal characters>
```

The runner service prints the same fingerprint. A user must compare every character and explicitly confirm the key before the control plane issues a worker capability. Creating a replacement runner registration clears the confirmation. A key change therefore requires another comparison and confirmation.

Each client connection creates a fresh X25519 static keypair in browser memory. The private key is never sent to Electron, the Python helper, or the hosted service, and the JavaScript buffer is overwritten after the Noise implementation imports it. The client public key is bound into the signed capability.

The runner also pins the control plane's raw Ed25519 capability-verification key at registration. A later key change stops heartbeats and relay authorization until the runner is deliberately re-registered. This prevents an unnoticed control-plane signing-key change from widening authority after enrollment.

## Capability

The authenticated account asks for the minimum scopes and a session byte limit. The control plane signs an Ed25519 compact token with this protected header:

```json
{"alg":"EdDSA","typ":"YINSHI-RUNNER-CAP","v":1}
```

The payload has exactly these claims:

| Claim | Meaning |
|---|---|
| `aud` | Fixed value `yinshi-runner`. |
| `sub` | Internal account identifier. |
| `runner_id` | Target runner identifier. |
| `runner_key` | Confirmed runner X25519 public key. |
| `initiator_key` | One-time client X25519 public key. |
| `transfer_id` | Random UUIDv4 used only for relay routing. |
| `jti` | Random capability identifier consumed once by the runner. |
| `scopes` | Sorted, unique subset of the protocol scope allowlist. |
| `protocol` | Fixed value `yinshi-runner-v1`. |
| `iat`, `exp` | Integer issue and expiration times with an exact 300-second lifetime. |
| `max_frame_bytes` | Fixed value 65,535. |
| `max_session_bytes` | Shared bidirectional ciphertext budget. |
| `v` | Integer protocol version `1`. |

A relay grant stores the SHA-256 hash of the capability, routing identifiers, expiry, and byte limit. It never stores the token or transferred frames. The first client WebSocket text frame presents the capability to claim that grant. The first Noise handshake payload presents the same token to the runner. The runner verifies the signature, expiry, runner ID, runner key, scope set, and limits, then compares the authenticated Noise initiator key with `initiator_key`.

The runner consumes `jti` in an owner-only SQLite replay database using an immediate transaction and a unique primary key. Consumption happens after signature and initiator-key verification but before the responder handshake message. A process restart does not make the capability reusable.

## Relay transport

The runner opens an outbound WebSocket at `/runner/relay` with its bearer token in the `Authorization` header. The browser opens `/api/runner/relay/<transfer_id>` and sends the capability as its first text frame. Capabilities and bearer tokens never appear in a URL.

The relay sends these control messages to the runner:

```json
{"runner_id":"<runner-id>","type":"welcome"}
{"transfer_id":"<uuid>","type":"open"}
{"transfer_id":"<uuid>","type":"close"}
```

Data traveling between the relay and runner is binary:

```text
16-byte UUID || Noise handshake message or transport ciphertext
```

Data traveling between the relay and client omits the UUID because the WebSocket is already transfer-specific. The relay checks frame length, queue depth, per-runner connection count, and the shared session byte budget. It forwards bytes unchanged. A slow client fills at most 16 queued frames before that transfer is closed.

Runner revocation deletes bearer authority and closes the active runner WebSocket and attached clients immediately on the process that owns the connection. The hosted process acquires an owner-only, non-blocking relay lock beside the control database. A second API process on the same deployment host fails startup instead of silently splitting runner and client sockets. Multi-host deployment still requires load-balancer affinity or distributed relay coordination.

## Noise handshake

The browser is the IK initiator and already knows the fingerprint-confirmed runner static key. The runner is the responder. Both sides use the ASCII prologue `yinshi-runner-v1`.

The two messages follow the standard IK pattern:

```text
<- s
...
-> e, es, s, ss, payload
<- e, ee, se, payload
```

The initiator payload is the signed capability. The responder payload contains exactly:

```json
{"protocol":"yinshi-runner-v1","transfer_id":"<uuid>"}
```

The capability and response therefore participate in the Noise transcript hash. The client rejects a responder payload whose protocol or transfer ID differs from the grant.

Python uses `noiseprotocol` 0.3.1. The browser uses `@richardhopton/noise-c.wasm` 0.5.0, a WebAssembly build of noise-c. Yinshi does not implement the Noise cipher state or handshake primitives itself. Tests on both sides use the canonical `Noise_IK_25519_ChaChaPoly_SHA256` vector and assert the first message, second message, transcript hash, and both transport directions byte-for-byte.

## Encrypted RPC

After the handshake, the client sends ordered request objects. Version 2 separates query values from the normalized path so file names never affect route authorization:

```json
{
  "body": null,
  "method": "GET",
  "path": "/health",
  "query": {},
  "request_id": "<uuid>",
  "sequence": 0,
  "type": "request",
  "v": 2
}
```

The runner requires sequence zero for the first request and increments by one. Noise transport nonces independently enforce ciphertext ordering. A request must match an allowlisted method, normalized path, bounded scalar query, body shape, and capability scope before dispatch. Version 1 requests without `query` remain accepted for the original health contract; responses use the request's version.

A response repeats the request ID and sequence:

```json
{
  "body": {"protocol":"yinshi-runner-v1","status":"ok"},
  "request_id": "<uuid>",
  "sequence": 0,
  "status": 200,
  "type": "response",
  "v": 2
}
```

The worker dispatches only these reviewed route families:

| Scope | Operations |
| --- | --- |
| `worker.health` | Worker health. |
| `repository.read`, `repository.write` | Repository list, import, update, and deletion. Imports accept credential-free HTTPS remotes only. |
| `workspace.read`, `workspace.write` | Workspace list, creation, state changes, and deletion. |
| `session.read`, `session.write` | Session CRUD, messages, and file tree metadata. |
| `session.stream` | Durable prompt start, ordered event polling, and idempotent cancellation. |
| `files.read`, `files.write` | Bounded workspace tree, changed-file, preview, diff, and content operations. |
| `terminal` | Account-scoped terminal attach, input, resize, restart, bounded output polling, and detach. |
| `provider.configure` | Provider catalog, encrypted provider connection storage, and provider OAuth start/poll/manual-callback operations. Secret values are never returned. |
| `pi.configure` | Pi configuration import, sync, category selection, commands, release notes, and deletion. |

Provider OAuth authorization URLs, flow identifiers, manual callback input, and status responses remain inside the Noise channel. Only the browser opens the returned credential-free HTTPS authorization URL.

Prompt events are written to the account's SQLCipher database with contiguous sequence numbers. A reconnect resumes from an exact cursor. A runner restart marks orphaned runs `interrupted`, appends a generic terminal event, and resets their sessions to idle. Terminal channels keep a bounded in-memory output journal. A worker or sidecar restart closes those channels; the client reports the interruption and opens a new shell instead of claiming that the old process survived.

Pi archive uploads use a 24,000-byte chunk protocol over one long-lived Noise session. The runner reserves at most 50 MiB per upload and 100 MiB in aggregate, accepts exact chunk retries, verifies the declared byte count and SHA-256 digest, clears the assembly buffer, and expires abandoned uploads after 15 minutes.

## Failure behavior

Authentication, JSON shape, scope, sequence, size, and decryption failures close only the affected transfer when its UUID is known. Unknown routing frames or malformed runner control messages close the shared runner connection because they indicate a relay protocol violation. A failed Noise or RPC session cannot be reused.

Clients close one-shot connections after one response. Chunked uploads reuse one ordered Noise session and reconnect with an exact chunk retry when transport fails. Every connection closes before 1,048,576 messages in either direction. Yinshi starts a fresh handshake rather than calling the Noise rekey operation because the browser wrapper does not expose rekey. Nonce exhaustion is therefore unreachable under the enforced threshold.

No error path logs capabilities, bearer values, frame bytes, decrypted requests, response bodies, paths, or user labels. Logs may contain random runner identifiers, HTTP status codes, and fixed operation names.

## Test vector provenance

The interoperability test uses the `IK` vector from the `Noise_25519_ChaChaPoly_SHA256` canonical vector set maintained by the `trancee/noise-protocol` project. The same bytes are processed by the browser WebAssembly implementation and the Python implementation. The repository tests also cover malformed key encoding, low-order keys, truncated messages, changed authentication tags, replayed transport ciphertext, wrong initiator keys, expired and altered capabilities, durable capability replay, out-of-order RPC sequences, relay byte limits, queue routing, key replacement, and immediate revocation.
