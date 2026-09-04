# Thread Orchestration Contract

Status: Frozen for Phases 0 and 1.

Source: `yinshi-thread-orchestration-plan.md`, based on commit `e18c86948f533ffb002bd6ca46118b8ee3fcaafb`.

This release adds database records and read APIs. It does not create child worktrees or run delegated agents.

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

## Deferred work

Phase 2 adds child worktree provisioning. Phase 3 adds manual spawn and orchestration writes. Phases 4 and 5 add sidecar protocol support and agent tools.

Later work adds cancellation, retry, reporting, recursive deletion, managed runtime integration, nested delegation, budgets, waiting, and automated result sealing.
