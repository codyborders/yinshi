# Thread Orchestration Contract

Status: Frozen through Phase 3. Phase 4 duplex sidecar protocol is implemented.

Source: `yinshi-thread-orchestration-plan.md`, based on commit `e18c86948f533ffb002bd6ca46118b8ee3fcaafb`.

This release adds database records, read and write APIs, isolated child workspaces, manual delegation, prompt execution, cancellation, retry, and results. It also adds the Phase 4 duplex sidecar bridge with one harmless ping operation. No Phase 5 model tools are enabled.

## Terms and ownership

A thread is an existing Yinshi session used as one reasoning or work unit. A root thread has no parent delegation. A child thread is referenced by `thread_delegations.child_session_id`.

A delegation records the durable parent-to-child task relationship. A result stores a bounded report with Git metadata. A workspace remains the filesystem and Git execution environment.

Each thread has exactly one session. Each child has at most one parent. Existing sessions without delegation records remain valid roots.

Parentage comes only from `thread_delegations.child_session_id`. The `sessions` table does not store a parent ID. Public APIs never reparent existing sessions.

Parent and child must share one tenant, repository, and runtime location. Backend code enforces depth and count limits. Bounded reads reject cycles and excessive trees.

A workspace cannot be deleted while one of its sessions owns a child delegation. The API returns HTTP 409 with `thread_children_present`.

## Limits and flags

| Setting | Default |
| --- | ---: |
| `THREAD_MAX_DEPTH` | 1 |
| `THREAD_MAX_DIRECT_CHILDREN` | 4 |
| `THREAD_MAX_ACTIVE_DESCENDANTS` | 4 |
| `THREAD_MAX_TOTAL` | 20 |
| `THREAD_MAX_SPAWNS_PER_TURN` | 4 |
| `THREAD_WAIT_TIMEOUT_SECONDS_MAX` | 60 |
| `THREAD_SNAPSHOT_MAX_FILES` | 20,000 |
| `THREAD_SNAPSHOT_MAX_BYTES` | 1 GiB |
| `THREAD_PROVISIONING_STALE_SECONDS` | 600 |

`THREAD_HIERARCHY_ENABLED` defaults to `true`. Disabled hierarchy routes return 404. `AGENT_DELEGATION_ENABLED` defaults to `false`.

All numeric limits must be positive. Maximum depth cannot exceed 32. Total capacity must exceed direct-child and active-descendant capacity because it includes the root.

## State rules

Delegations start in `provisioning`. They may advance through `queued` and `running` before reaching a terminal state.

Valid terminal states are `completed`, `failed`, `cancelled`, and `interrupted`. Cancellation can use `cancelling` between `running` and `cancelled`.

Active counts include `provisioning`, `queued`, `running`, and `cancelling`. A provisioning record without a child session reserves capacity and appears as a placeholder.

Later recursive deletion must remove deepest descendants first. Parent workspaces are removed last. Phase 1 rejects parent deletion instead.

## Database contract

Shared databases use schema version 6. Per-user databases use user version 2. Both migration paths support SQLite and SQLCipher.

Migrations are additive and repeatable. Existing rows remain unchanged. Older restored databases migrate when opened. Whole-file backup and relay transfer require no format conversion.

Existing tables gain these fields:

```sql
ALTER TABLE sessions ADD COLUMN title TEXT;
ALTER TABLE workspaces ADD COLUMN kind TEXT NOT NULL DEFAULT 'user';
ALTER TABLE workspaces ADD COLUMN parent_workspace_id TEXT;
```

Workspace kinds are `user`, `delegated`, and `integration`. Existing rows use `user`.

`thread_delegations` stores parent and child session IDs, child workspace ID, request data, model choices, status, Git base data, timestamps, retry lineage, and safe errors.

The child session ID is unique. Parent deletion uses `ON DELETE RESTRICT`. Child deletion cascades to its delegation. Parent and idempotency key form a unique pair.

`thread_results` uses the delegation ID as its primary key. It stores versioned JSON lists, summary text, Git refs, seal state, and timestamps.

Result deletion follows delegation deletion. Public result reads return sealed rows only.

## Read API

Thread routes use the same authenticated database and ownership checks as session routes.

| Method | Route | Response |
| --- | --- | --- |
| `GET` | `/api/threads/{session_id}` | One thread projection |
| `GET` | `/api/threads/{session_id}/tree` | Root, descendants, placeholders, and counts |
| `GET` | `/api/threads/{session_id}/children` | Direct child projections |
| `GET` | `/api/threads/{session_id}/result` | Sealed child result |
| `GET` | `/api/threads/{session_id}/limits` | Configured limits and current usage |

A thread projection contains IDs, depth, title, role, origin, state, workspace ID, model, child counts, spawn allowance, and creation time.

Roots use session status and `user` origin. Delegated children use delegation status and initiator origin.

A tree request accepts any member ID. The response always identifies the actual root and returns the complete bounded tree. Nodes preserve their actual parent ID.

Counts include placeholders because each placeholder reserves a possible child. Limit calculations use totals from the actual root, even when requested for a child.

A result request returns 404 when no sealed result exists. Existing session routes keep their prior behavior and gain optional title input and output.

Responses use Pydantic models. Tree expansion stops after 500 descendants or placeholders. Ancestry traversal stops after 32 parent links.

## Security and failure behavior

Tenant mode uses one database per user. Legacy mode applies repository ownership checks before returning thread data.

A malformed cross-owner relationship cannot disclose foreign session metadata. Unknown sessions and unauthorized records return 404.

Requests cannot supply tenant IDs, runtime locations, filesystem paths, branch names, Git refs, parent workspace IDs, credentials, or capabilities.

Errors do not include raw database, filesystem, or credential details. Cycles and oversized trees stop processing instead of returning partial data.

The deletion guard runs before cancellation, sidecar release, container removal, worktree deletion, or session-file deletion. A second service check protects direct callers. Foreign keys provide the final storage check.

## Isolated workspace service

`ThreadWorkspaceService` provides child provisioning, Git finalization, and idempotent cleanup. Phase 3 will call these operations after reserving a delegation.

Clean parents use their exact `HEAD`. Dirty parents use a synthetic commit built through a private alternate Git index.

Dirty snapshots include tracked changes and permitted untracked files. Ignored files remain excluded. Protected `.env` paths cause rejection.

Tracked symlinks remain Git symlink blobs. Snapshot creation never follows their targets. Dirty submodules cause rejection.

Snapshot commits use `refs/yinshi/snapshots/<delegation-id>`. Result commits use `refs/yinshi/results/<delegation-id>`.

Hidden refs use atomic create-only publication. A competing writer keeps ownership of its ref.

Child branches use `yinshi/thread-<short-delegation-id>`. Worktrees start from the exact base commit and receive `kind = delegated` with their parent workspace ID.

Provisioning holds the repository lifecycle lock. It preserves parent `HEAD`, branch, index, staged state, working files, and untracked files.

Cleanup removes only artifacts owned by its delegation. It preserves pre-existing branches, worktrees, and result refs after failed provisioning.

Finalization captures committed and uncommitted child state in one synthetic result commit. Its parent is the recorded base commit.

Result refs are immutable. A retry reuses a result only when its tree and parent match.

Changed-file output supports additions, modifications, deletions, copies, and renames. It stops above 5,000 entries.

Child-only cleanup and finalization reject ordinary user workspaces. Repository and workspace roots cannot use symlink indirection.

## Manual orchestration writes

| Method | Route | Behavior |
| --- | --- | --- |
| `POST` | `/api/threads/{session_id}/children` | Reserve, provision, attach, and optionally start one child |
| `POST` | `/api/threads/{thread_id}/cancel` | Cancel an attached child or provisioning placeholder |
| `POST` | `/api/threads/{session_id}/retry` | Create a distinct sibling retry with lineage |
| `POST` | `/api/threads/{session_id}/report` | Insert or update one bounded result draft |

Creation uses a canonical UUID idempotency key. The same parent, key, and normalized request return stable state. Changed input conflicts.

Reservation uses `BEGIN IMMEDIATE`. Depth, direct-child, active-descendant, and total-tree capacity are checked in the reservation transaction.

Git provisioning and prompt-journal operations run without an open orchestration database connection. Attachment uses a short compare-and-set transaction.

Cancellation supports queued, running, and provisioning work. Provisioning cancellation claims the state before cleanup. Cleanup removes only artifacts owned by that delegation.

Retry accepts failed, cancelled, or interrupted children. It creates new delegation, workspace, session, prompt run, and retry lineage records.

Stale provisioning records become interrupted before cleanup. Reconciliation runs before Phase 3 writes and uses `THREAD_PROVISIONING_STALE_SECONDS`.

Result reports use optimistic versions. Version zero inserts version one. Matching updates increment the version. Exact stale replay returns the current draft.

A terminal child can seal an existing draft. Git finalization runs after database closure. The result ref is immutable, and database sealing uses compare-and-set.

## Frontend contract

The session page keeps `/app/session/{id}` as its canonical route. Runtime-qualified IDs remain attached to their source runtime.

The Threads panel shows the bounded tree, placeholders, lifecycle states, child actions, capacity, and sealed results. Desktop uses a side panel. Narrow screens use an overlay.

The child dialog supports bounded task context, role, model, thinking, and start behavior. It blocks duplicate submission and reports capacity or server errors.

The Sidebar groups delegated workspaces under their owning repository. It shows delegation status and omits destructive workspace actions for delegated entries.

## Duplex sidecar orchestration protocol (Phase 4)

Implemented. Pi custom tools run inside the Node sidecar. The Python backend keeps tenant authorization, durable data, limits, provisioning, and scheduling. The per-query Unix socket now also carries a private duplex channel.

### Capability

- The backend creates one random capability per prompt run. See `orchestration_bridge.generate_orchestration_capability`.
- The token binds to one tenant, runtime, session, prompt run, connection, and expiry. Default expiry is 30 minutes.
- `SidecarClient.query` claims the capability once for its connection. Teardown revokes the capability. A later query cannot reuse it.
- The token reaches the sidecar only in memory, inside query options. It never enters prompts, files, environment variables, logs, event journals, or telemetry.
- The sidecar opens one RPC channel per query. It clears the channel when the query ends. A reused Pi session never keeps a stale token.

### Wire frames

Request, sidecar to backend:

```json
{
  "type": "orchestration_request",
  "id": "session-id",
  "request_id": "uuid",
  "capability": "opaque-token",
  "operation": "ping_thread_bridge",
  "arguments": {"message": "bounded echo"}
}
```

Success and error responses, backend to sidecar:

```json
{
  "type": "orchestration_response",
  "id": "session-id",
  "request_id": "uuid",
  "ok": true,
  "result": {"status": "ok", "echo": "...", "session_bound": true}
}
```

```json
{
  "type": "orchestration_response",
  "id": "session-id",
  "request_id": "uuid",
  "ok": false,
  "error": {"code": "capability_invalid", "message": "..."}
}
```

### Protocol rules

1. Request frames are limited to 64 KiB. Response frames are limited to 256 KiB. Oversized frames fail closed.
2. Frame validation is strict. The field set is exact. Strings are bounded. Arguments must be objects. Unknown fields fail closed.
3. Operations come from a fixed allowlist. Phase 4 ships one operation: `ping_thread_bridge`. It echoes a bounded 256-character message. It touches no database, filesystem, network, or credential.
4. A duplicate in-flight `request_id` closes the offending query and connection. Completed IDs leave the pending map. Repeating completed IDs may repeat harmless ping.
5. One connection permits at most 16 handler tasks. Capacity exhaustion closes the query and connection without creating rejection tasks.
6. Handler execution stops at 60 seconds with `handler_timeout`. Handler exceptions return `handler_failed` with a generic message. Logs contain fixed messages, not request content.
7. Error codes are fixed: `invalid_request`, `request_too_large`, `unknown_operation`, `invalid_arguments`, `capability_invalid`, `capability_expired`, `session_mismatch`, `duplicate_request`, `too_many_requests`, `handler_timeout`, `handler_failed`, `response_too_large`.
8. One lock serializes every connection write. Handler responses never interleave with stream frames.
9. Orchestration frames are consumed before any event yield. They never reach the model event stream, the prompt journal, or replay. Ordinary Pi `tool_use` and `tool_result` events stay visible.
10. Query teardown and disconnects cancel and drain handler tasks. Drained handlers never answer. The Node pending map then rejects with `orchestration_disconnected`.
11. The Node side permits 16 pending requests and times out at 60 seconds. Replies must match the request, session, and originating socket.
12. Tool registration is conditional. Queries with a capability register `thread_bridge_ping`. Queries without one do not. Overlapping bridge queries cannot replace an active owner.

Frame byte limits include the envelope and newline. Tool cancellation rejects pending calls immediately. Query cleanup removes timers and abort listeners.

Phase 5 mutations will require domain-level idempotency. Phase 4 does not retain completed request IDs or replay results.

### Phase 4 exit

A test Pi tool can call the harmless backend operation. The path stays free of deadlock, credential leakage, and journal pollution. Phase 5 tools (`spawn_thread`, `list_children`, `get_thread`, `wait_for_threads`, `cancel_thread`, `report_thread_result`) remain absent.

## Deferred work

Phase 5 adds model-facing thread tools and the terminal observer.

Recursive deletion, budgets, agent-origin spawning, and waiting remain deferred.
