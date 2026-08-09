# Backend Software Design Review

Scope: `backend/src/yinshi/`, 23,124 lines across 76 Python files. The `backend/.venv` tree was excluded. The lens is the red-flag checklist from "A Philosophy of Software Design".

The task named `/Users/user/projects/yinshi/plan.md` and `/Users/user/projects/yinshi/progress.md`. Neither file exists in the repository. The `plans/` directory holds dated planning documents instead. This review therefore works from source code only. No files were modified.

## Size distribution

| Layer | Files | Lines | Share |
|---|---|---|---|
| `api/` | 19 | 5,601 | 24% |
| `services/` | 36 | 11,976 | 52% |
| root modules | 21 | 5,532 | 24% |

Ten files hold 8,721 lines, which is 38% of the backend.

| File | Lines |
|---|---|
| `api/stream.py` | 1193 |
| `api/auth_routes.py` | 1188 |
| `services/pi_config.py` | 1087 |
| `services/container.py` | 914 |
| `db.py` | 876 |
| `services/runners.py` | 808 |
| `models.py` | 735 |
| `runner_agent.py` | 710 |
| `services/sidecar_runtime.py` | 663 |
| `services/workspace_files.py` | 538 |

## 1. The api layer is not a thin pass-through over services

The table below counts lines that precede the first route decorator. Those lines are helper code living in the transport layer.

| File | Total lines | Routes | Private helpers | Lines before first route | Helper share |
|---|---|---|---|---|---|
| `api/stream.py` | 1193 | 2 | 20 | 770 | 65% |
| `api/terminals.py` | 426 | 1 | 9 | 272 | 64% |
| `api/auth_routes.py` | 1188 | 19 | 20 | 453 | 38% |
| `api/repos.py` | 363 | 5 | 7 | 120 | 33% |

Raw SQL statement counts inside `api/` reach 16 in `api/stream.py`, 14 in `api/repos.py`, and 12 in `api/sessions.py`. The remainder are 7 in `api/auth_routes.py`, 6 in `api/workspaces.py`, and 2 in `api/deps.py`. There is no `services/session.py` and no `services/repo.py`. Those two resources have no service layer.

Seven pieces of business logic live in `api/` rather than in `services/`.

Prompt-to-workspace-name summarization sits at `api/stream.py:388-484`. That block holds a 13-entry filler-prefix list and a 70-entry stop-word set. The `_summarize_prompt` function follows at lines 486-513. This rule shapes the product name that users see. It does not belong in transport code.

Thinking-level policy sits at `api/stream.py:266-384`. Four helpers there perform 119 lines of model-capability negotiation.

Legacy Pi-context gating sits at `api/stream.py:571-595`. That helper counts stored messages and updates the `pi_context_version` column directly.

The join query across sessions, workspaces, and repos sits at `api/stream.py:597-628`. It is written twice, once for tenant mode and once for legacy mode.

Sidecar execution setup spans 141 lines at `api/stream.py:630-770`. That function resolves the provider connection. It also refreshes and re-persists OAuth secrets, resolves git credentials, and remaps container paths.

Turn persistence spans `api/stream.py:882-1120`, and it owns the stored-turn schema. It writes message rows with batched partial content. It then writes a final full-message value and a turn status.

The GitHub App install-flow state machine writes to the control database from three route-layer sites. Those sites are `api/auth_routes.py:118-135`, lines 700-718, and lines 748-802. Filesystem tree walking sits at `api/sessions.py:53-72`.

Severity is high. `api/stream.py` is the largest file in the backend, and 65% of it is non-route code.

Restructuring. Add `services/prompt_turn.py`. Move `_summarize_prompt`, the catalog helpers, `_clamp_thinking_level`, and `_build_effective_settings` into it. Move the two stored-turn helpers and the message-persistence statements as well. Add `services/session_store.py` alongside `services/repo_store.py`. Give them every statement against sessions, messages, and repos now written inline. Move `_resolve_execution_context` to `services/execution_context.py`. That function already depends only on services. The target for `api/stream.py` is about 250 lines.

## 2. Services modules leak persistence details in both directions

Several modules get this right. Six of them open `get_control_db()` internally and return dataclasses or plain dictionaries. They are `services/pi_config.py`, `services/keys.py`, `services/runners.py`, `services/accounts.py`, `services/provider_connections.py`, and `services/desktop_auth.py`. Callers never see a raw database row from those modules. A clean example sits at `services/pi_config.py:304-448`. Encryption of the label, URL, and error columns happens inside the insert, update, and read helpers. Callers do not know those columns are encrypted.

Six leaks work against that pattern.

`services/prompt_journal.py` depends on the API layer. Line 15 imports `fastapi.Request`. Line 17 imports `yinshi.api.deps.get_db_for_request`. Line 64 imports `yinshi.api.stream.prompt_session`. Every public method takes a request parameter, at lines 105, 191, and 245. The module cannot be used outside an HTTP request. Severity is high.

`services/sidecar_runtime.py:17` imports `fastapi.Request`. The only reason is reaching the container manager on application state at lines 648-663. That manager lives for the whole process, so request state is the wrong home for it. Severity is medium.

`api/deps.py:29-44` returns a raw database connection. Every route and the prompt journal write SQL against it. The persistence decision is hidden nowhere. It is the shared currency of the whole application. Severity is high, and this is the root cause of finding 1.

`tenant.py:23` imports the private `_open_connection` helper from `db.py`. Severity is low on its own. It does signal that the two files are one module split in two, as finding 6 describes.

`services/pi_config.py:25` imports four names from `services/git`, and three of them are private. They are `_git_askpass_env`, `_run_git`, and `_validate_clone_url`. Severity is medium. Those belong behind one public git facade. Line 28 of the same file imports a private settings sanitizer from `services/user_settings`. That is a low-severity instance of the same habit.

Restructuring. Replace the connection currency with a `TenantStore` object. Create it once per request and pass it by dependency injection. Give the prompt journal a store factory plus a container manager reference. Apply the same treatment to `sidecar_runtime`.

## 3. Layering is not coherent, and three inversions exist

The intent is that `api` calls `services`, and `services` calls `db`. The actual graph differs.

`services/prompt_journal.py:64` calls the FastAPI route function `api.stream.prompt_session`. Lines 65-87 then parse the SSE text that route produced back into dictionaries. That code splits on the blank-line separator, strips the data prefix, and calls `json.loads`. So `services` calls `api`, which calls `services`. A text wire format is serialized and then deserialized inside one process. Severity is high. Any change to SSE framing in `api/stream.py` silently breaks the durable journal. The docstring at line 63 admits the coupling, since it reads "Adapt the existing SSE route generator".

`api/deps.py:9` imports `get_db` from `db.py`. Meanwhile `services/prompt_journal.py:17` imports a helper from `api/deps.py`. The module graph therefore contains a cycle between the two packages.

The `api/` package skips `services/` for four resources. Repos, workspaces, sessions, and messages are all manipulated with inline SQL. The four route files involved are `api/repos.py`, `api/workspaces.py`, `api/sessions.py`, and `api/stream.py`. Terminals, workspace files, Pi config, provider connections, runners, and desktop auth do use services. Layering therefore covers about half the domain.

Restructuring. Extract the shared prompt-execution generator into `services/prompt_turn.py`. Have it return an async iterator of event dictionaries. Let `api/stream.py` wrap that iterator in SSE framing. Let the prompt journal consume the same iterator directly. This removes the cycle and the text round trip together.

## 4. Crypto, Noise, and relay code is mostly free of duplication

This cluster is the best-factored part of the backend.

`services/crypto.py` is 249 lines and is a deep module. Lines 47-58 hold one HKDF derivation. Lines 213-243 hold one AEAD envelope pair. Lines 104-190 hold one wrapped-DEK envelope pair. `services/control_encryption.py` does not duplicate any of it. That module is a thin policy layer, adding AAD binding at lines 34-48 and key-rotation overlap at lines 13-28. The layer is justified. `services/keys.py:11-21` delegates all primitives to `crypto.py`.

Five relay modules hold five distinct concerns, and no implementations overlap. `services/runner_noise.py` holds the Noise IK responder. `services/runner_noise_session.py` holds the session, the replay store, and the rekey logic. `services/runner_relay.py` holds the broker plus transfer grants. `services/runner_agent_relay.py` holds the agent-side runtime. `services/runner_rpc.py` holds request parsing, scope checks, and dispatch.

Two small blemishes exist. One shared wire constant is copied, since `_RELAY_FRAME_BYTES_MAX` appears at `services/runner_relay.py:17` and again at `services/runner_agent_relay.py:16`. Hoisting it is worthwhile at low severity. Separately, `services/keys.py:25-30` embeds a MiniMax pricing table inside a generic key-management module. That is a special-general mixture at low severity, and it belongs in `model_catalog.py`.

The exception is severe. `services/runner_rpc.py:20-54` plus lines 132-222 hold a second copy of the FastAPI route table. Thirty-plus path regexes and a 91-line `_required_scope` function re-encode every worker URL. Adding one route to `api/` requires a matching edit here. Otherwise the route is silently unreachable over the relay, and nothing enforces the correspondence. Severity is high, and the red flag is information leakage.

Restructuring. Attach the required scope to each route as FastAPI dependency metadata. Build the scope table at startup by walking the registered routes. `_required_scope` then becomes a dictionary lookup, and an unregistered route fails loudly at startup.

## 5. Runtime resolution is not duplicated, but the naming implies a family that does not exist

The five modules named for runtime cover five unrelated concerns.

| Module | Lines | Actual concern |
|---|---|---|
| `desktop_runtime.py` | 164 | Electron helper process: loopback socket bind, readiness pipe, Uvicorn boot |
| `worker_runtime.py` | 168 | In-process ASGI dispatcher with method, path, and query validation |
| `services/sidecar_runtime.py` | 663 | Container mounts, home directory layout, Pi session paths, lifecycle wrappers |
| `services/git_runtime.py` | 69 | One ephemeral GitHub App credential payload |
| `services/workspace_runtime_paths.py` | 78 | Workspace and repo host-path validation |

There is no shared code here and no duplicated logic. The problem is the opposite. The word runtime is a vague name applied to five different things. A reader must open each file to learn what it does. Severity is medium, and the red flag is vague names.

Restructuring. Rename each module for its concern. One candidate set follows.

| Current name | Proposed name |
|---|---|
| `desktop_runtime.py` | `desktop_helper_process.py` |
| `worker_runtime.py` | `worker_asgi_dispatch.py` |
| `services/sidecar_runtime.py` | `services/sidecar_container_layout.py` |
| `services/git_runtime.py` | `services/github_git_credentials.py` |
| `services/workspace_runtime_paths.py` | `services/workspace_paths.py` |

The real duplication in this area sits between `services/sidecar_runtime.py` and `api/stream.py`. `services/workspace_runtime_paths.py:57-78` already encapsulates one sequence. It repairs the checkout and loads workspace and repo paths. It then proves tenant ownership and installs secret guardrails. Three callers use it, at `api/terminals.py:302`, `api/terminal_channels.py:86`, and `api/workspace_files.py:74`. `api/stream.py` re-implements the same sequence inline. The three inline pieces are a checkout call at line 790, `_lookup_session` at line 597, and `_validate_workspace_path` at line 530. Three of four callers use the general mechanism. Severity is medium, and the red flag is repetition.

## Additional findings

### 6. The db.py and tenant.py modules are one module split in two. Severity high.

`tenant.py:66-152` holds `USER_SCHEMA_SQL`, and `db.py:33-120` holds `SCHEMA_SQL`. A diff of the two regions reports one deletion and no other change. The 88-line block in `db.py` becomes the 87-line block in `tenant.py` by dropping the owner-email column.

Eighty-seven of eighty-eight schema lines are duplicated verbatim. That covers six create-table statements, six indexes, and three triggers. Every column addition must therefore be made twice.

The migration logic is duplicated too, and it has already drifted. `db.py:342-384` applies five numbered migrations behind a `schema_version` table. `tenant.py:153-175` applies four of the same column additions. It tracks no version and re-runs `PRAGMA table_info` on every call. `db.py:379` adds `pi_context_version` with a default of zero, and `tenant.py:172-174` does the same. Both base schemas declare a default of one, at `db.py:66` and at `tenant.py:97`. New databases and migrated databases therefore start in different states. That is exactly the condition `api/stream.py:571-595` exists to detect.

Six SQLCipher helpers are near-duplicates.

| Concern | `db.py` | `tenant.py` |
|---|---|---|
| Driver module names | 25 | 29-32 |
| Driver loading | 130-140 | 183-204 |
| Keyed connection open | 164-191 | 216-247 |
| Plaintext readability probe | 193-206 | 257-270 |
| Encrypted integrity validation | 208-228 | 299-313 |
| Plaintext-to-encrypted migration | 230-293 | 315-343 |

The readability probes at `tenant.py:257-270` and `db.py:193-206` are identical apart from one file-existence call.

Restructuring. Create `db/schema.py`. Give it one parameterized schema builder plus one ordered migration list keyed by version. Apply both to each database kind. Create `db/sqlcipher.py` holding the driver loader, the keyed open, the plaintext probe, the encrypted validator, and the migration. Parameterize those by a key-derivation callable. The two current modules then hold only policy and connection scoping. Expect well under the current combined 1,305 lines.

### 7. The get_user_db helper runs DDL on every connection. Severity medium.

`tenant.py:421-431` calls `_ensure_user_db_schema` inside the connection context manager. Every database access therefore runs an 87-line script, four table-info queries, and a commit. `api/deps.py:29-44` opens a fresh connection for each request-scoped block. `api/stream.py` opens eight such blocks per prompt request. Those blocks sit at lines 788, 807, 821, 845, 857, 989, 1027, and 1075. A reader of `get_user_db` will not guess that reading one row triggers schema DDL. The red flag is nonobvious code.

Restructuring. Move `_ensure_user_db_schema` into `init_user_db` only. Cache initialized database paths in a process-level set. The prompt journal already does exactly this at `services/prompt_journal.py:113`.

### 8. Five near-identical container lifecycle wrappers. Severity medium.

`services/sidecar_runtime.py:559-645` defines five functions. They are named for touch, begin activity, end activity, protect, and release. Each has the same seven-line body. Each calls `_tenant_container_manager`, returns on a missing manager, then calls `_call_container_method` with a name string. That is about 87 lines expressing one idea five times.

`_call_container_method` at line 516 returns silently when the method is missing. A typo in the name string therefore becomes a no-op with no log line. `ContainerManager` is a concrete class at `services/container.py:51`, so the duck typing buys nothing.

`services/sidecar_runtime.py:528-557` already provides `tenant_container_activity`. That async context manager pairs begin with end, and it optionally protects. One call site uses it, at `api/terminals.py:346`. Seven call sites hand-roll the pair instead. They sit at `api/catalog.py:79`, `api/stream.py:690`, `api/stream.py:896`, `api/auth_routes.py:959`, `api/auth_routes.py:989`, `api/auth_routes.py:1053`, and `api/settings.py:269`. Each one must remember the matching end call in a cleanup block. The pairing is manual and unenforced.

Restructuring. Delete the five wrappers. Keep `tenant_container_activity` and add a lease variant. Convert the seven call sites. Type the manager as a concrete optional value and call its methods directly.

### 9. Provider OAuth flow logic repeated three times in routes. Severity medium.

Three routes share one 15-line skeleton. They sit at `api/auth_routes.py:952-981`, at lines 984-1046, and at lines 1049-1119. Each resolves the socket, begins container activity, creates the sidecar connection, and acts. Each ends with a cleanup block that ends activity, touches the container, and disconnects. The status branching in the two callbacks is nearly identical, at lines 1002-1030 and at lines 1058-1082. A complete status persists the credential and releases the lease. An error status releases the lease and returns 400. Any other status protects the container and returns 202.

Restructuring. Add `services/provider_auth.py` exposing a start call, a poll call, and a submit call. Return a small result dataclass carrying status, payload, and lease action. The three routes then shrink to about ten lines each.

### 10. Seven exception types mapped one-to-one to status codes. Severity medium.

`services/desktop_auth.py:23-49` defines seven exception classes. Ten catch blocks in `api/auth_routes.py` do nothing but produce a status code and a message. They sit at lines 462-484, at lines 509-527, and at lines 532-573. Inside `exchange_desktop_authorization_code` at lines 532-573, error paths occupy 27 of 41 lines. The success path is 6 lines. The red flag is too many exceptions.

Restructuring. Give the base error a status attribute and a detail attribute. Collapse the ten blocks into one handler. As an alternative, merge `DesktopCodeInvalidError` with `DesktopPkceMismatchError`. Line 551 already handles them identically.

### 11. Runner storage-profile table duplicated verbatim. Severity high.

`runner_agent.py:57-121` and `services/runners.py:58-120` hold the same `RunnerStorageProfileSpec` dataclass with 11 fields. Both also hold the same 63-line `_STORAGE_PROFILES` dictionary with three profile entries. A diff of the two regions reports only a blank line, a closing brace, and a one-line docstring difference. Every profile value is identical.

The eight supporting constants are duplicated as well, at `runner_agent.py:48-55` and at `services/runners.py:40-47`. So is the storage-profile literal type, at `runner_agent.py:33-37` and at `services/runners.py:24-28`. So is `_storage_profile_spec`, at `runner_agent.py:182` and at `services/runners.py:157`.

The control plane validates reported runner capabilities against this table at `services/runners.py:313`. The agent uses the same table to choose its own defaults at `runner_agent.py:212-216`. Editing one copy alone makes runners fail validation against the control plane, with no local signal. The red flag is information leakage.

Restructuring. Move the literal type, the constants, the dataclass, the dictionary, and the lookup helper into `runner_storage_profiles.py`. Import from both sides. This deletes about 130 duplicated lines.

### 12. Secret-file rules and excluded-directory lists duplicated. Severity medium.

The rule for dot-env files appears in four independent encodings. Two constants sit at `services/workspace_files.py:44-45`. The `_is_secret_path` function sits at lines 113-124 of the same file. A shell regex named `_SECRET_PATH_GREP` sits at line 50 and is embedded in generated git hooks. A fourth encoding, `_is_sensitive_filename`, sits at `services/pi_config.py:159-165`.

The excluded-directory list is duplicated and has already diverged. `api/sessions.py:21-35` lists 12 names, while `services/workspace_files.py:20-42` lists 22. Both walkers cap at 5000 entries under different constant names. They are `_TREE_FILE_LIMIT` at `api/sessions.py:36` and `_MAX_TREE_ENTRIES` at `services/workspace_files.py:46`. The session tree endpoint at `api/sessions.py:169-187` therefore shows different content than the workspace-files tree for one directory.

Restructuring. Create `services/path_policy.py`. Give it the secret path predicate, the excluded directory names, and the tree entry cap. Add one helper that generates the shell pattern for the git hooks. Delete `api/sessions.py:21-72` and call the shared walker.

### 13. Encrypted control-field list repeated in three places. Severity medium.

The field tuple naming the label, URL, and error columns appears at `db.py:842` for the migration backfill. It appears again at `services/pi_config.py:308` for decrypt on read. A third copy sits at `services/pi_config.py:417` for encrypt on update. The same values are spelled out individually at lines 377-378 for insert. Adding one encrypted column therefore requires four coordinated edits across two modules.

Restructuring. Declare the field set once in `services/pi_config.py` and export it. Have `db.py:809-862` call a function on that module rather than reaching into the table itself. The control-database migration currently knows the column-level encryption policy of a table owned elsewhere.

### 14. The pi_config module holds three unrelated responsibilities. Severity medium.

The file spans 1,087 lines and 60 functions across three concerns. Lines 304-449 hold control-database persistence with field encryption. Lines 155-297 and lines 451-748 hold archive handling plus secret scrubbing on the filesystem. Lines 1033-1087 hold runtime resolution for the sidecar.

The third concern is where the module is consumed, at `services/sidecar_runtime.py:22`. That is 55 lines depending on the other thousand. `resolve_agent_dir` at lines 1056-1059 is a pure pass-through over `resolve_pi_runtime`. It has zero callers anywhere in the repository.

Restructuring. Split into three modules. Put rows and encryption in `services/pi_config_store.py`. Put extraction, scrubbing, and category toggling in `services/pi_config_archive.py`. Put the three resolve functions in `services/pi_runtime.py`. Delete `resolve_agent_dir`.

### 15. ContainerManager is one class with 45 methods. Severity low.

The class spans `services/container.py:51-914`. Its public interface is deep and simple. It covers container creation, activity marking, lease protection, destruction, and idle reaping. Implementation complexity is correctly pulled downward.

Three sub-concerns are visible inside it. Lines 80-218 invoke podman processes. Lines 261-338 provision the image and the network. Lines 539-621 and lines 887-914 track idle state plus leases. Splitting is optional, and the current shape is defensible.

One real issue sits at lines 528-537. `_enforce_container_limit` reads the max count through a dynamic attribute lookup with a fallback. That setting is a declared field with a default at `config.py:130`. The defensive lookup hides a typo class.

### 16. Repeated fetch-then-recheck pattern in api/sessions.py. Severity low.

Four routes follow one shape. Each fetches the row, raises 404 when it is absent, then calls an owner check. That check issues a second query with a three-table join to read the owner email, at `api/deps.py:66-103`. The four sites sit at `api/sessions.py:113-120`, at lines 135-140, at lines 155-161, and at lines 170-184. This costs two queries per read where one suffices. The pattern repeats eight times across `api/sessions.py` and `api/workspaces.py`.

Restructuring. Have the store function return the row with its owner already joined. Perform the ownership check inside that function so callers cannot forget it.

### 17. Confusing security-mode naming. Severity low.

`config.py:256-261` defines `tenant_db_encryption_required`. That function returns false when the configured mode is the string for enabled. `config.py:264-266` defines `tenant_db_encryption_enabled`, which returns true for that same mode. The cause is `_mode_enabled` at lines 244-253. That helper maps both the enabled value and the required value to true. A first guess about the enabled branch will be wrong, so the code is nonobvious.

The configuration vocabulary offers four values, covering disabled, enabled, required, and auto. The function names offer only two of them.

Restructuring. Rename the modes so that each one reads unambiguously. Rename the two functions to match. Separately, `config.py:239-242` defines `_auth_is_enabled` as a pass-through kept for older tests. Update its callers and delete it.

### 18. Dead code in models.py. Severity low.

`models.py` spans 735 lines and 51 model classes. Three are unreferenced by any production module. `WSPrompt` at line 237 and `WSCancel` at line 253 are referenced only by `tests/test_models.py:56-68`. The WebSocket prompt transport they described no longer exists. `UserOut` at line 268 has zero references anywhere, including tests.

### 19. Import-time application construction. Severity low.

`main.py:489-490` builds the application and reads settings at module import. Importing that module for any reason builds middleware and validates settings. It can raise an error from `_validate_settings` at `config.py:287`. `services/prompt_journal.py:64` imports from `api/stream.py` lazily inside a function. That workaround exists to dodge this class of problem.

## What is already good

`services/crypto.py:47-243` holds one HKDF derivation and two envelope formats. The validation helpers are applied consistently, and the interface is much simpler than the implementation.

`services/desktop_auth.py:125-402` is genuinely deep. `exchange_desktop_authorization_code` takes three parameters. Behind them it hides PKCE verification, single-use code consumption, device creation, and token issuance.

`services/runner_rpc.py:267-376` holds a clean state machine. `EncryptedRunnerRpcSession` handshakes, then decrypts and dispatches and encrypts in order. A failure flag prevents reuse after an error.

`services/workspace_runtime_paths.py:57-78` is a correct deep abstraction. One call repairs the checkout, validates both paths against tenant storage, and installs guardrails.

`main.py:63-105` defines `RequestBodyLimitMiddleware`. It handles the declared content length and also the streamed case, which is commonly missed.

`config.py:287-348` defines `_validate_settings`, which fails closed at startup on dangerous combinations. One example is no-auth mode bound to a non-loopback host.

Test breadth is strong. 55 test files hold 16,199 lines. Dedicated suites cover the Noise handshake, the relay, the RPC path, crypto, containers, and security regressions.

Docstring discipline is consistent, and comments generally explain reasoning rather than restating code. `services/sidecar_runtime.py:437-441` is a good example. It explains why the repo is mounted at its host path so that git worktree pointers resolve.

## Priority order

| # | Finding | Severity | Effort |
|---|---|---|---|
| 6 | `db.py` and `tenant.py` schema and SQLCipher duplication | high | large |
| 3 | Prompt journal calls the SSE route and re-parses its text | high | medium |
| 1 | 770 lines of business logic in `api/stream.py` | high | large |
| 11 | Storage-profile table duplicated verbatim in two modules | high | small |
| 4 | Route table shadowed in `services/runner_rpc.py` | high | medium |
| 2 | Raw connections as the shared currency across layers | high | large |
| 8 | Five container lifecycle wrappers, plus an unused helper | medium | small |
| 12 | Secret rules and excluded-directory lists duplicated | medium | small |
| 13 | Encrypted control-field list repeated three times | medium | small |
| 9 | Provider OAuth skeleton repeated three times | medium | medium |
| 10 | Seven desktop exceptions and ten status-mapping blocks | medium | small |
| 14 | The pi_config module holds three responsibilities | medium | medium |
| 5 | Five unrelated modules named for runtime | medium | small |
| 7 | Schema DDL on every tenant connection | medium | small |
| 15 to 19 | Assorted low-severity items | low | small |

Findings 11 and 8 are the smallest high-value items. Both are mechanical deletions of about 220 duplicated lines. Start there.