# Software Design Review

| Field | Value |
| --- | --- |
| Date | 2026-08-07 |
| Scope | entire codebase, four components, roughly 40,000 source lines |
| Lens | "A Philosophy of Software Design" red-flag checklist |
| Status | findings only, nothing was implemented |

| Component | Path | Non-test lines |
| --- | --- | --- |
| Backend | `backend/src/yinshi/` | 23,124 |
| Frontend | `frontend/src/` | 11,053 |
| Desktop | `desktop/src/` | 3,168 |
| Sidecar | `sidecar/src/` | 2,584 |

Excluded from the review are `backend/.venv`, `node_modules`, `frontend/dist`, `desktop/dist`, and `desktop/release`.

## Findings index

| ID | Severity | Effort | Finding |
| --- | --- | --- | --- |
| B1 | high | small | The sidecar session map has no removal path. |
| B2 | high | medium | Two refresh paths race, and the loser revokes the device. |
| L1 | high | medium | The route table is written out in four separate places. |
| L2 | high | large | Two copies of the schema, plus migrations that no longer agree. |
| L3 | high | small | A 63-line profile dictionary copied between agent and control plane. |
| L4 | high | medium | Handshake constants restated by hand on each side of the wire. |
| L5 | high | small | Three inline copies of one runtime-selection rule. |
| L6 | mixed | small | Twelve smaller duplications. |
| L7 | medium | medium | No generated API types. |
| D1 | high | medium | No single owner for the signed-in user and its tokens. |
| D2 | high | large | Persistence divided by which file it opens. |
| D3 | medium | small | Two supervisors duplicate one process lifecycle. |
| F1 | high | medium | Sidebar.tsx holds six responsibilities, and one has a real bug. |
| F2 | high | large | The stream API module is 65 percent non-route code. |
| F3 | high | large | The sidecar module is thirteen responsibilities in one file. |
| F4 | high | medium | The desktop main module holds unrelated work. |
| F5 | medium | medium | Three more oversized components in the frontend. |
| F6 | medium | medium | Backend response formats are consumed raw in the UI. |
| P1 | medium | medium | Two terminal subsystems. |
| P2 | medium | small | Two agent-stream subsystems, one of which is unreachable. |
| P3 | medium | medium | Two journals with near-identical semantics. |
| P4 | medium | small | The encrypted upload path avoids the transport policy layer. |
| A1 | high | medium | A service calls a route and re-parses its own text output. |
| A2 | medium | none | The backend imports itself across a network hop. |
| A3 | medium | small | Two runtime vocabularies that do not line up. |
| A4 | medium | small | Five unrelated modules named for runtime. |
| S1 to S21 | mixed | small | Smaller findings, listed in Part 7. |

## Executive summary

The codebase has strong low-level design. Cryptographic primitives, the Noise
handshake, the secure JSON store, and the account lease verifier are deep
modules with simple interfaces. Test coverage is broad, at 55 backend test files
and 20 frontend test files.

The weakness is at the boundaries. The same design decision is written in
several places, and in several cases the copies have already drifted apart.
Three problems recur across every component.

- One decision has many homes. The route table exists in four copies spread over three languages. The database schema is written twice. The runner storage profile table is written twice. The default-runtime rule is written three times.
- Concepts are split by operation order rather than by information hiding. Two desktop modules divide the signed-in user between them, by the moment each is needed. The backend persistence code divides by which file it opens.
- Transport-layer files hold business logic. `api/stream.py` has 770 lines before its first route. `Sidebar.tsx` holds six unrelated responsibilities. `sidecar.js` holds thirteen.

Two findings are live defects rather than design debt. Both are small to fix.
They are B1, where the session map never evicts, and B2, where a concurrent
token refresh can revoke the user device.

## Part 1: live defects

### B1. The sidecar session map has no removal path

`sidecar/src/sidecar.js:934` creates `activeSessions`. It is written at `:1655`
and `:1739`, and read at `:1624`, `:1683`, and `:1966`. The only removal is
`clear()` at `:2005`, inside `cleanup()`, which runs at process exit.

There is no delete, no time-to-live, and no size cap. The protocol has no
dispose message, and `backend/src/yinshi/services/sidecar.py` never sends one.
Socket close detaches terminals at `:1129-1132` and leaves sessions untouched.

Each retained entry holds a live pi session with model context, a settings
manager, and an open session file handle. See `SessionManager.open` at `:205`.
In the desktop app the sidecar runs for the whole application lifetime. Every
session a user opens therefore stays resident until quit.

The inconsistency inside the same file is instructive. OAuth flows have a
30-minute time-to-live and a cap of 8, at `:35` and `:1368`. Terminals are
removed on exit at `:1017-1020`. Pi sessions alone have neither policy.

Suggested fix. Move the map into `pi/sessionRegistry.js` with a maximum entry
count and an idle time-to-live. On eviction, call `unsubscribe()` and
`piSession.dispose()`. Add a `session_release` message so the backend can
release deterministically.

### B2. Two refresh paths race, and the loser revokes the device

The backend treats a replayed refresh token as a compromise signal. At
`backend/src/yinshi/services/desktop_auth.py:244-255`, presenting an
already-rotated token revokes the whole device. It then calls
`signal_desktop_device_revoked`.

The desktop app has two independent paths that spend the stored refresh token.
Only one of them is serialised.

- `HostedAccessSession.getAccessToken` at `desktop/src/hostedAccessSession.ts:57-84` de-duplicates concurrent refreshes through a `#refresh` promise. It guards stale writes through `#epoch`. This path is correct.
- `DesktopAppController.switchProfile` at `desktop/src/appController.ts:110-121` calls `resumeAccount`. That is wired at `desktop/src/main.ts:301-309` to call `resumeDesktopAccount` directly. This path never touches `#refresh`.

Both eventually reach `desktop/src/accountSession.ts:88-99`, which posts the
stored refresh token.

The two are wired 25 lines apart in the same function.
`desktop/src/main.ts:285-292` builds `HostedAccessSession` with a callback into
`resumeDesktopAccount`. Then `main.ts:301-309` builds a second closure calling
the same function and pushing the result back in. The same operation appears
twice, in two directions.

A renderer request that triggers a refresh through `hostedApiGateway` can
overlap a profile switch. The second refresh presents a token the first has
already rotated. The backend then revokes the device, and the user is signed out
with no explanation.

The suggested fix is in Part 3, finding D1.

## Part 2: one decision, many homes

This is the dominant structural problem. Each item below is information leakage
in the strict sense. A single design decision that a reader must find in several
files, kept consistent only by hand.

### L1. Every API route is enumerated four times, in three languages

| Location | Purpose | Patterns |
| --- | --- | --- |
| `backend/src/yinshi/api/*.py` | the actual FastAPI routers | 81 routes |
| `backend/src/yinshi/services/runner_rpc.py:132-215` | server-side scope enforcement | 21 |
| `frontend/src/runtime/runtimeTransport.ts:46-206` | client-side scope selection | 18 |
| `desktop/src/hostedApiGateway.ts:12-192` | Electron bridge allowlist | 20 |

Adding one route now requires four coordinated edits across three languages.
Nothing enforces the correspondence, and no test asserts that the tables agree.

The copies have already drifted. The backend handles four pi-upload path
patterns at `runner_rpc.py:206-215`. The frontend table has no upload entries at
all, so `runtimeTransport.ts` would reject them. The upload flow works only
because it bypasses the transport layer entirely. See finding P4.

Each table has a defensible reason to exist. The backend table is authoritative
enforcement. The desktop table limits what a compromised renderer can reach.
Deleting either one is therefore the wrong fix.

Recommended change. Declare the route-to-scope mapping once, as FastAPI dependency
metadata attached to each route. Build the backend table at startup by walking
the registered routes. An unregistered route then fails loudly instead of
becoming silently unreachable. Generate the frontend and desktop tables at build
time from the OpenAPI document. Divergence then becomes a build failure rather
than a runtime rejection.

### L2. The database schema is written twice, and the migrations have drifted

`backend/src/yinshi/db.py:33-120` holds `SCHEMA_SQL`. `tenant.py:66-152` holds
`USER_SCHEMA_SQL`. A direct diff of the two regions reports exactly one
difference. `tenant.py` omits the `owner_email` column.

Eighty-seven of eighty-eight lines are duplicated verbatim. That covers six
create-table statements, six indexes, and three triggers. Every column addition
must be made twice.

The migration logic is duplicated too, and it has already produced inconsistent
state. `db.py:342-384` applies five numbered migrations behind a `schema_version`
table. `tenant.py:153-175` applies four of the same column additions with no
version tracking, re-running `PRAGMA table_info` on every call. Both migrations
add `pi_context_version` with a default of zero, at `db.py:379` and
`tenant.py:172-174`. Both base schemas declare a default of one, at `db.py:66`
and `tenant.py:97`. New databases and migrated databases therefore start in
different states.

That inconsistency is real rather than hypothetical. `api/stream.py:571-595`
exists to detect and repair it at request time.

Six SQLCipher helpers are near-duplicates as well.

| Concern | `db.py` | `tenant.py` |
| --- | --- | --- |
| Driver module names | 25 | 29-32 |
| Driver loading | 130-140 | 183-204 |
| Keyed connection open | 164-191 | 216-247 |
| Plaintext readability probe | 193-206 | 257-270 |
| Encrypted integrity validation | 208-228 | 299-313 |
| Plaintext to encrypted migration | 230-293 | 315-343 |

The two probes at `tenant.py:257-270` and `db.py:193-206` differ only by one
file-existence call.

How to fix it. Create `db/schema.py` with one parameterised schema builder and
one ordered migration list keyed by version, applied to both database kinds.
Create `db/sqlcipher.py` holding the driver loader, keyed open, plaintext probe,
integrity validator, and migration, parameterised by a key-derivation callable.
The two current modules then hold only policy and connection scoping. Expect
well under the current combined 1,305 lines.

### L3. The runner storage profile table is duplicated verbatim

`runner_agent.py:57-121` and `services/runners.py:58-120` hold the same
`RunnerStorageProfileSpec` dataclass with 11 fields, and the same 63-line
`_STORAGE_PROFILES` dictionary with three entries. A direct diff reports only a
one-line docstring difference and a blank line. Every profile value is identical.

The eight supporting constants are duplicated at `runner_agent.py:48-55` and
`services/runners.py:40-47`. So is the storage-profile literal type, at
`runner_agent.py:33-37` and `services/runners.py:24-28`. So is
`_storage_profile_spec`, at `runner_agent.py:182` and `services/runners.py:157`.

The control plane validates reported runner capabilities against this table at
`services/runners.py:313`. The agent uses the same table to choose its own
defaults at `runner_agent.py:212-216`. Editing one copy alone makes runners fail
validation against the control plane, with no local signal.

Proposed restructuring. Move the literal type, constants, dataclass, dictionary, and
lookup helper into `runner_storage_profiles.py`. Import from both sides. This
deletes about 130 duplicated lines and is the cheapest high-value change in the
report.

### L4. The Noise wire protocol is re-derived independently per language

There is no shared schema, interface definition, or generated artifact. Matching
literal constants are written twice, in two different crypto stacks. Python uses
`noiseprotocol` at `services/runner_noise.py:11-20`. The browser uses
`@richardhopton/noise-c.wasm` at `crypto/noiseIk.ts:1-6`. Parity is maintained
only by hand.

| Constant | Backend | Frontend |
| --- | --- | --- |
| Protocol name | `runner_noise.py:22` | `noiseIk.ts:8` |
| Max message length 65535 | `runner_noise.py:26` | `noiseIk.ts:11` |
| IK first message overhead 96 | `runner_noise.py:24` | `noiseIk.ts:12` |
| IK second message minimum 48 | `runner_noise.py:25` | `noiseIk.ts:13` |
| Rehandshake threshold 1048576 | `runner_noise_session.py:24` | `noiseIk.ts:14` |
| Prologue tag `yinshi-runner-v1` | `runner_noise_session.py:23` | `encryptedRunnerClient.ts:9` |

A protocol change requires coordinated edits in seventeen files across backend
and frontend, plus their test mirrors.

Tracing this raised two open discrepancies. The frontend request
envelope sends `v: 2` at `encryptedRunnerClient.ts:483`. The backend accepted key
set is named `_REQUEST_KEYS_V1` at `runner_rpc.py:17`, and it omits the `query`
key the frontend always sends. Both deserve confirmation. Separately, the session
byte cap has three values. It is 64 KiB at `encryptedRunnerClient.ts:15`,
256 KiB at `runtimeTransport.ts:90`, and 128 MiB at `encryptedUpload.ts:10`.

What to do. Declare the constants and envelope schema in one machine-readable
file. Generate the Python and TypeScript constant modules from it. This will not
unify the two crypto implementations, and it should not try to. It removes the
class of failure where the two sides disagree about a number.

### L5. The default-runtime rule is written three times

`frontend/src/runtime/runtimeRef.ts:62-67` exports `defaultRuntimeRef`, which is
the correct home for this decision. Only `parseRuntimeResourceId` calls it. Three
sites reimplement it inline, at `Sidebar.tsx:172-174`, `Settings.tsx:604-606`,
and `Settings.tsx:697`.

The related BYOC usability predicate is written four times, and one copy is
wrong. See finding F1.

More broadly, `window.yinshiDesktop` is referenced 27 times across 9 non-test
modules. `RuntimeRef.location` is inspected 29 times across 9 non-test modules.
Some of those inspections are presentation code that should not care about
transport. `WorkspaceInspector.tsx:321` hides a download link for `byoc`.
`Sidebar.tsx:516` collapses BYOC repositories by default. Both should read a
capability flag on the transport instead.

A better structure. Add `runtime/environment.ts` exporting `isDesktop()`. Route all
default-runtime construction through `defaultRuntimeRef`. Add
`runtime/useAvailableRuntimes()` as the single producer of the runtime list,
consumed by both `Sidebar` and `Settings`.

### L6. Smaller duplications

Each row below states one rule that is written in more than one place.

| Severity | Duplicated item | Where it lives |
| --- | --- | --- |
| medium | Secret-file rule, encoded four times | `workspace_files.py:44-45`, `:113-124`, `:50` for the shell regex, and `pi_config.py:159-165`. |
| medium | Excluded-directory list, already divergent, so the two tree endpoints disagree today | 12 names at `api/sessions.py:21-35` against 22 at `workspace_files.py:20-42`. |
| medium | Encrypted control-field list, spelled out a fourth time at `:377-378` | `db.py:842`, `pi_config.py:308`, and `:417`. |
| medium | Sidecar newline-JSON framing | `api/terminals.py:72-91` and `services/terminal_journal.py:478-512`. |
| medium | Desktop launch config, where only the packaged path has tests | `main.ts:130-163` for development and `runtimeLaunchConfig.ts:165-176` for packaged. |
| medium | Profile directory layout | `main.ts:93-105`, `runtimeLaunchConfig.ts:104-152`, and `main.ts:524-527`. |
| medium | Runtime-secret validation, with identical regexes | `runtimeSecrets.ts:20-46` and `runtimeLaunchConfig.ts:72-82`. |
| low | Relay frame byte cap | `runner_relay.py:17` and `runner_agent_relay.py:16`. |
| low | The `DesktopProfileSummary` type | `credentialStore.ts:22-26` and `desktopApi.ts:29-33`. |
| low | The `RESOURCE_ID_PATTERN` constant | five frontend modules. |
| low | base64url encoding | `encryptedRunnerClient.ts:85-96`, `runtimeRef.ts:17-27`, and `encryptedUpload.ts:22-26`. |
| low | The `formatTimestamp` helper, with identical bodies | `Settings.tsx:22-27`, `CloudRunnerSection.tsx:113-118`, and `PiConfigSection.tsx:22-27`. |

### L7. No generated API types

There is no OpenAPI code generation. `frontend/package.json` has no generate
script. The backend serves `/openapi.json` only when `debug` is true, at
`main.py:451`. Twenty-two request and response types are hand-written twice,
in `backend/src/yinshi/models.py` and `frontend/src/api/client.ts`. The terminal
channel types are duplicated outside those two files, as are the prompt run
types and the run status enum.

This ranks below L1 through L5 because the failure mode is a compile-time
type error where the two definitions meet, rather than silent divergence. It is
listed because it is the root cause of finding F6.

## Part 3: concepts split by operation order

Temporal decomposition organises code around the order in which things happen,
rather than around what each module knows. Three instances were found, and each
one affects real behaviour.

### D1. The desktop account is split across two modules by timing

`accountSession.ts` covers startup and token expiry. `hostedAccessSession.ts`
covers the time between those moments. Neither owns the account. Both manipulate
the same three facts, which are the active profile, the access token, and the
expiry.

This is the direct cause of defect B2.

Recommended change. Collapse both into one `DesktopAccount` class. It owns the
profile, the token, the expiry, and one serialising queue. Every path that spends
that token then waits on the same promise. The replay condition is
therefore defined out of existence rather than defended against. The public
methods would be `profile`, `resume()`, `accessToken()`, `adopt(session)`, and
`clear()`.

Keep the pure parts where they are. `verifyAccountLease` stays in
`accountLease.ts`. `startHostedSignIn` and `readHostedDesktopTokenResponse` stay
in `hostedAuth.ts`. The offline-lease fallback at `accountSession.ts:50-70` and
`:100-106` becomes private state of `DesktopAccount`. Offline eligibility is an
account question rather than a transport question.

The net effect is that two modules and two closures become one module and one
field.

### D2. The db and tenant modules are one module split by database kind

Covered as finding L2. It is named again here because the underlying problem is
temporal decomposition and not merely duplication. The split follows which
database is being opened, and it ignores what each module knows. A shared
`db/schema.py` and `db/sqlcipher.py` would restore information hiding.

A related consequence sits at `tenant.py:421-431`. `get_user_db` calls
`_ensure_user_db_schema` inside the connection context manager. Every database
access therefore runs an 87-line DDL script, four `PRAGMA table_info` queries,
and a commit. `api/stream.py` opens eight such blocks per prompt request, spread
between line 788 and line 1075. A reader of `get_user_db`
will not guess that reading one row triggers schema DDL. The correct pattern
already exists at `services/prompt_journal.py:113`, which caches initialised
database paths.

### D3. Two supervisors, one process lifecycle

| Concern | `sidecarSupervisor.ts` | `helperSupervisor.ts` |
| --- | --- | --- |
| Liveness check | 73, 196-198 | 24-26, 124-126 |
| Wait for exit with timeout | 71-86 | 28-42 |
| SIGTERM, wait, SIGKILL, wait, throw | 88-100 | 44-59 |
| Spawn, await readiness, kill on failure | 154-186 | 99-121 |
| Memoised stop | 189-203 | 127-131 |

About 60 lines of parallel logic. The copies have drifted in three ways.
`helperSupervisor.ts:48-49` registers the exit listener before SIGTERM, while
`sidecarSupervisor.ts:95-96` sends SIGTERM first. Both are correct today, so a
reader must verify the ordering twice. `SidecarOptions.args` is
`readonly string[]` at `:9` while `StartManagedHelperOptions.arguments` is
`string[]` at `:17`. `localRuntime.ts:44-52` copies arrays to bridge that
difference. The error messages differ, at `:99` and `:57`.

The way out. Extract `childProcessLifecycle.ts` with a `startSupervisedChild`
function. Parameterise it by an `awaitReadiness` callback, plus `onStarted` and
`onStopped` hooks. The sidecar keeps its stale socket removal, permission
assertion, and stdout readiness reader. The helper keeps its descriptor-3 reader.
343 lines become roughly 230.

## Part 4: files that hold too much

### F1. Sidebar.tsx holds six responsibilities, and one has a real bug

1,203 lines, 26 `useState` calls, 7 effects, 5 components, 13 raw `/api/...`
literals.

| Number | Responsibility | Lines |
| --- | --- | --- |
| 1 | Multi-runtime discovery and fan-out fetch | 188-259 |
| 2 | GitHub installation state and OAuth callback handling | 97-129, 261-308 |
| 3 | Repository AGENTS.md editing | 522-525, 589-628, 695-757 |
| 4 | Workspace and session lifecycle | 546-573, 845-873 |
| 5 | Application chrome, covering theme, settings, logout, inline SVG | 398-483 |
| 6 | Repository import classification and validation | 1008-1063 |

The name describes a screen region. It does not describe a unit of information
hiding.

The bug sits inside responsibility 1. The BYOC runner usability predicate is
written in four places under three different rules. Only `resolveRuntime.ts`
checks every condition.

| Location | Runner exists | ID matches | Rejects revoked | Key confirmed | Key present |
| --- | --- | --- | --- | --- | --- |
| `runtime/resolveRuntime.ts:31-38` | yes | yes | yes | yes | yes |
| `pages/Settings.tsx:751-755` | no | no | yes | yes | yes |
| `components/Sidebar.tsx:222` | no | no | missing | yes | yes |
| `components/CloudRunnerSection.tsx:358, 378, 401` | no | no | missing | yes | yes |

Because `Sidebar.tsx:222` omits the revoked check, a revoked runner still
produces a `byoc` entry in the runtime list at `:225-229`. It also still lists
its repositories at `:232-243`. The user can select it and attempt an import. In
the same session, `Settings.tsx` would already have hidden that option.

Suggested fix, in order. First add `runtime/runnerIdentity.ts` exporting
`loadPairedRunnerRuntime()`, consumed by all four sites. That is a small change
that closes a real inconsistency. Then move runtime discovery into
`useRuntimeRepositories()`. Move the AGENTS.md editor into
`components/RepoSettingsForm.tsx`, and move the install handling into
`hooks/useGitHubInstallations.ts`. Merge `createBranch` with
`openOrCreateSession` into one `startSession` helper. Target is `Sidebar.tsx`
under 300 lines holding layout and composition only.

### F2. The stream API module is 65 percent non-route code

`api/stream.py` runs 1,193 lines. The first route decorator is at line 771. That
leaves 770 lines of helper code living in the transport layer, plus 16 raw SQL
statements.

| Business logic in the route layer | Lines |
| --- | --- |
| Workspace-name summarisation, with a 13-entry filler list and 70-entry stop-word set | 388-513 |
| Thinking-level policy across four helpers | 266-384 |
| Legacy pi-context gating, writing `pi_context_version` directly | 571-595 |
| Session join query, written twice for tenant and legacy modes | 597-628 |
| Sidecar execution setup, covering provider resolution, OAuth refresh, git credentials, path remapping | 630-770 |
| Turn persistence, owning the stored-turn schema | 882-1120 |

The summarisation rule determines the workspace name a user sees. That is
product behaviour written into an SSE endpoint.

The same pattern holds elsewhere in the layer. `api/terminals.py` has 272 lines
before its first route, which is 64 percent. `api/repos.py` has 14 raw SQL
statements and `api/sessions.py` has 12. There is no `services/session.py` and
no `services/repo.py`. Those two resources therefore have no service layer at
all. Layering covers about half the domain.

The root cause is `api/deps.py:29-44`, which returns a raw database connection.
Persistence became a shared type passed between every layer instead of a hidden
decision.

Proposed restructuring. Add `services/prompt_turn.py` for summarisation, catalogue
helpers, thinking-level clamping, and turn persistence. Add
`services/session_store.py` and `services/repo_store.py`. Move
`_resolve_execution_context` to `services/execution_context.py`, since it
already depends only on services. Replace the raw-connection dependency with a
`TenantStore` object. Target for `api/stream.py` is about 250 lines.

### F3. The sidecar module is thirteen responsibilities in one file

`sidecar.js` runs 2,021 lines. One class, `YinshiSidecar` at `:932-2021`, holds
1,090 lines, 32 methods, and three independent registries at `:934-936`. Above
it sit 900 lines of module-level helpers. The class name describes a process
where it should describe a concept.

| Number | Responsibility | Approximate size |
| --- | --- | --- |
| 1 | Socket server, chmod, health-log timer | 90 |
| 2 | Newline framing and connection state | 35 |
| 3 | Request dispatch over 20 message types | 69 |
| 4 | Terminal registry and pty lifetime | 250 |
| 5 | Pi session creation and reconciliation | 120 |
| 6 | Prompt execution and pi-event translation | 289 |
| 7 | Model catalog and reference resolution | 180 |
| 8 | Provider auth normalisation | 140 |
| 9 | OAuth flow registry and manual-input relay | 260 |
| 10 | Pi resource listing with mtime cache | 175 |
| 11 | Extension UI adapter and passthrough theme | 142 |
| 12 | Installed-package version lookup | 50 |
| 13 | dotenv loading and settings normalisation | 110 |

Terminal handling shares no state or logic with OAuth flows or with prompt
execution. Those three share only a socket.

The worst single function is `processQuery` at `:1675-1963`, which runs 289
lines. It contains a finalisation state machine with five mutable flags at
`:1750-1790`. It also contains a 139-line translation switch over ten pi event
types at `:1791-1929`. Its error handling at `:1943-1962` reads
`entry.cancelRequested`, which `cancelSession` writes at `:1970`. Meanwhile
`cancelSession` depends on `processQuery` clearing that flag at `:1932` and
`:1954`. Neither method can be understood alone.

Two smaller structural issues remain. The wire field `id` has four different
meanings inside `handleRequest` at `:1147-1215`. It is a pi session identifier,
a terminal identifier, a workspace identifier fallback, and a request
correlation identifier. That vague name forced eleven fallback expressions.
Separately, the same error wrapper appears eleven times. The sites are `:1221`,
`:1230`, `:1239`, `:1249`, `:1262`, `:1278`, `:1295`, `:1321`, `:1350`, `:1468`,
and `:1527`. That is about 90 duplicated lines.

Suggested decomposition into fifteen modules, leaving `sidecar.js` as a
composition file of roughly 60 lines. Recommended order.

- Extract `pi/eventTranslator.js` and `pi/runFinalizer.js`. Highest complexity per line, no I/O, immediately testable.
- Extract `pi/sessionRegistry.js` with eviction. This also fixes defect B1.
- Extract `router.js` with one shared error wrapper. This removes 90 duplicated lines and enables the `id` rename.
- Extract `auth/oauthFlows.js`. Tests already exist in `sidecar/tests/oauth-flow-limits.test.js`.
- Extract `terminals/terminalRegistry.js`. Tests already exist in `sidecar/tests/terminal.test.js`.

### F4. The desktop main module holds unrelated work around a clean orchestrator

`main.ts` runs 713 lines with no test file. `configureApplication` spans
`:266-607`, which is 342 lines in one function. It holds thirteen
responsibilities. Those cover shell environment curation, profile directory
naming, and development executable discovery. They also cover a second
launch-configuration implementation, window security wiring, and IPC
authorisation policy. The remainder are eight IPC handlers, update dialog copy,
and application lifecycle events.

`DesktopAppController` at `appController.ts:36-149` is the real orchestrator. It
is clean, with an explicit dependency contract and one operation queue. `main.ts`
holds whatever remained after that extraction stopped halfway.

The clearest example is the local repository import handler at `:502-605`, which
runs 104 lines of business flow inside an IPC handler. It asks the user for a
directory, clones it, then confirms whether to proceed when the repository is
dirty. It then makes a second clone attempt and an HTTP call to the local helper.
Finally it validates the response and deletes the clone on failure. Composition roots do not hold rollback logic at
`:466-481` or dialog copy at `:548-560`.

Suggested fix, by payoff. Add `profileLayout.ts`, which also fixes the L6 layout
duplication. Add `repositoryImportFlow.ts` with injected dependencies, making
the flow testable without Electron. Add `ipcRouter.ts` for the sender guard at
`:237-264` and the eight registrations. Those currently repeat a four-line guard
and rewrap, which is about 30 duplicated lines. Add `signInFlow.ts` for
`:310-345`. Fold `developmentLaunchConfig` at `:130-163` into
`buildRuntimeLaunchConfig` with a `mode` parameter. Target is `main.ts` under 150
lines.

### F5. Three more oversized components in the frontend

`pages/Settings.tsx` runs 968 lines with 24 `useState` calls and 5 top-level
responsibilities. It also contains a second oversized component inside itself.
`ProviderCard` at `:66-511` holds 15 `useState` calls and mixes three auth
mechanisms. `resetOauthFlowState` at
`:103-112` and `applyOauthFlowState` at `:114-131` exist only to keep 7 related
state values consistent. That is the signal for one state object.

`ProviderCard` also has a runtime risk. The OAuth polling loop at `:227-241`
runs up to 600 iterations at one second each. It is not bound to an
`AbortController` or to unmount. Only the `connecting` flag guards re-entry.
After unmount, `setError` and `applyOauthFlowState` still fire, and up to 600
requests still go out.

`components/CloudRunnerSection.tsx` runs 837 lines with 17 `useState` calls and
4 concerns. Those are provisioning, Noise identity pairing, connectivity
diagnostics, and a BYOC repository browser. The fourth duplicates what
`Sidebar.tsx:218-243` already does. A 53-line static option catalogue at
`:31-83` interleaves display copy with behaviour.

`components/WorkspaceInspector.tsx` runs 757 lines with 12 `useState` calls and
7 raw `/api/...` literals.

The shared root cause is F6.

### F6. Backend response formats are consumed raw in the UI

`models/sessionModels.ts` maps only model refs and thinking levels, in 169 lines.
Everything else reaches components unchanged. Ninety-eight snake_case wire-field
accesses were counted across seven UI files. `Settings.tsx` has 24,
`PiReleaseNotesSection.tsx` has 19, and `Sidebar.tsx` has 17.
`CloudRunnerSection.tsx` has 16, `PiConfigSection.tsx` has 10,
`ToolCallBlock.tsx` has 8, and `Session.tsx` has 4.

Sixty raw `/api/...` URL literals sit across 16 modules, and 32 of them are
inside `components/` or `pages/`.

The correct pattern already exists in this codebase.
`runner/repositories.ts:9-24`, `runtime/promptStream.ts:67-124`, and
`runtime/terminalChannel.ts:46-59` all validate and map where wire data enters
the application. That discipline is simply absent for settings, runners, repos,
and message history.

What to do. Add one mapper module per resource. Runners need one, and so do
repos and stored messages. Then add named resource functions such as
`fetchRepos(transport)` so components stop carrying URL literals. This change
does more than any other to shrink `Sidebar.tsx` and `WorkspaceInspector.tsx`.

## Part 5: parallel subsystems

Four cases were found where two implementations of one thing coexist. Each one
doubles the maintenance cost. Each one also creates a class of bug where a fix reaches only
one path.

### P1. Two terminal subsystems

`api/terminals.py:273` exposes a direct WebSocket proxy with its own sidecar
framing helpers at `:72-91`. `api/terminal_channels.py:121-250` exposes a polling
channel backed by `services/terminal_journal.py`, with its own framing at
`:478-512`. Both are registered at `main.py:395-396`. Terminal event validation
exists in Python at `terminal_journal.py:404-454` and again in TypeScript at
`terminalChannel.ts:47-59`, with the same size bounds written in both.

### P2. Two agent-stream subsystems, one of which is unreachable

The SSE path runs through `api/client.ts:514-579` and `api/stream.py`. The polled
journal path runs through `runtime/promptStream.ts` and `api/prompt_runs.py`.
Both share `normalizeEvent` and duplicate transport, status enums, and
cancellation.

The SSE branch appears to be dead. `useAgentStream.ts:127` and `:193` select it
when `runtimeTransport` is falsy. In `pages/Session.tsx:66-70`, `id` and
`transport` both derive from the same `runtimeResource` object. They are
therefore undefined together, and `sendPrompt` returns early at
`useAgentStream.ts:47`. That makes roughly 90 lines dead. It also doubles the
branching a reader must follow. Confirm before deleting.

### P3. Two journals with near-identical semantics

`services/prompt_journal.py` at 523 lines and `services/terminal_journal.py` at
537 lines both implement sequenced batch accumulation with the same replay
contract.

### P4. The encrypted upload path avoids the transport policy layer

`runtime/runtimeTransport.ts:322-341` special-cases uploads.
`runtime/encryptedUpload.ts:150-186` calls `connectEncryptedRunner` directly. It
hard-codes `scopes: ["pi.configure"]` at `:159`, skipping `requiredScope`,
`parseByocPath`, and `validateRuntime`. The result is two independent BYOC
request paths under three different session limits.

The guard is also inert. `runtimeTransport.ts:323-338` validates one allowed
upload path, `/api/settings/pi-config/upload`. `encryptedUpload.ts:99-131` never
sends that path. It sends `/uploads`, then `/uploads/{id}/chunks/{i}`, then
`/uploads/{id}/complete`. The validated value is discarded immediately.

A better structure. Give `RuntimeTransport` an explicit `uploadPiConfig(file)` method
so the path stops being a caller concern. Move the session-byte constants into
one `runtime/limits.ts`.

## Part 6: architectural inversions

### A1. A service calls a route and re-parses its own text output

`services/prompt_journal.py:64` calls `yinshi.api.stream.prompt_session`, which
is a FastAPI route function. Lines 65-87 then parse the SSE text that route
produced back into dictionaries. They split on the blank-line separator, strip
the data prefix, and call `json.loads`.

So `services` calls `api`, which calls `services`. A text wire format is
serialised and immediately deserialised inside one process. Every public method
on the journal takes a `Request` parameter, at `:105`, `:191`, and `:245`. The
module therefore cannot be used outside an HTTP request.

Any change to SSE framing in `api/stream.py` silently breaks the durable journal.
The docstring at `:63` acknowledges the coupling, reading "Adapt the existing SSE
route generator".

The module graph also contains a cycle. `api/deps.py:9` imports from `db.py`,
while `services/prompt_journal.py:17` imports from `api/deps.py`.

Suggested fix. Extract the shared prompt-execution generator into
`services/prompt_turn.py`, returning an async iterator of event dictionaries.
`api/stream.py` wraps it in SSE framing. The journal consumes it directly. This
removes the cycle and the text round trip together.

### A2. The backend imports itself across a network hop

This is recorded for clarity rather than as a defect.

The BYOC runner runs a second full FastAPI application in-process.
`runner_worker.py:264-293` calls `main.create_app` through a deferred import.
`worker_runtime.py:29-40` then dispatches into it over ASGI with no listening
socket. One codebase acts as both control plane and remote worker, separated
only by `app.state.mode`.

This is a deliberate and reasonable choice, because it guarantees that the
worker implements exactly the same routes. It appears here because the deferred
import at `runner_worker.py:264` is the only thing preventing a circular import.
A new reader will also not expect it.

### A3. Two runtime vocabularies that do not line up

The frontend `RuntimeRef.location` union holds `local`, `hosted`, and `byoc`, at
`runtime/runtimeTransport.ts:10-17`. The backend `AppMode` holds `desktop`,
`hosted`, and `worker`, at `main.py:415-457`. Frontend `local` maps to backend
`desktop`. Frontend `byoc` maps to backend `worker`. Deployment material uses a
third vocabulary that shares no terms with either union.

Nothing in the code states or enforces the mapping, and no file records it.

Backend mode branching occurs in twelve places. Those include a distinct
middleware stack at `main.py:309-322`, a fixed `desktop-local` tenant at
`:344-352`, a different content security policy at `:361-363`, and a router split
at `:379-412`.

The remedy. Choose one vocabulary. Failing that, record the mapping in one file
and reference it from both sides.

### A4. Five unrelated modules named for runtime

| Module | Lines | Actual concern |
| --- | --- | --- |
| `desktop_runtime.py` | 164 | Electron helper process, covering socket bind, readiness pipe, Uvicorn boot |
| `worker_runtime.py` | 168 | In-process ASGI dispatcher with validation |
| `services/sidecar_runtime.py` | 663 | Container mounts, home layout, pi session paths |
| `services/git_runtime.py` | 69 | One ephemeral GitHub App credential payload |
| `services/workspace_runtime_paths.py` | 78 | Workspace and repo host-path validation |

There is no shared code and no duplicated logic here. The problem is the
opposite. The word runtime is a vague name applied to five different things. A
reader must open each file to learn what it does. The same word also names the
frontend `runtime/` directory and the desktop `runtimeSecrets.ts`. Neither of
those relates to any of the above.

On the desktop side, `runtimeSecrets.ts` is worth renaming to `helperKeyStore.ts`.
`RuntimeSecretStore` at `:48-89` has no link to the user account. It generates
three local encryption keys for the Python helper. Its current name places it
beside `credentialStore` and `accountSession` in a reader's mind.

## Part 7: smaller findings

These items are worth fixing, and none of them blocks the larger work above.

| ID | Severity | Finding |
| --- | --- | --- |
| S1 | medium | Five near-identical container lifecycle wrappers sit at `sidecar_runtime.py:559-645`, which is about 87 lines expressing one idea. `_call_container_method` at `:516` returns silently on a missing method, so a typo becomes a no-op with no log. |
| S2 | medium | `tenant_container_activity` at `sidecar_runtime.py:528-557` pairs begin with end, and only 1 of 8 call sites uses it. The other seven write the pair by hand, in `catalog.py`, `stream.py`, `auth_routes.py`, and `settings.py`. |
| S3 | medium | The provider OAuth flow is repeated three times, at `auth_routes.py:952-981`, `:984-1046`, and `:1049-1119`. Each copy is about 15 lines with near-identical status branching. |
| S4 | medium | Seven exception types at `desktop_auth.py:23-49` map one-to-one to status codes across ten catch blocks in `auth_routes.py:462-573`. In one function, error paths occupy 27 of 41 lines against a 6-line success path. |
| S5 | medium | `services/pi_config.py` holds persistence, archive handling, and runtime resolution in 1,087 lines. `resolve_agent_dir` at `:1056-1059` is a pure pass-through with zero callers. |
| S6 | medium | `pi_config.py:25` imports three private names from `services/git`, and it imports a private sanitizer from `user_settings` at `:28`. |
| S7 | medium | Session reuse is decided by a `JSON.stringify` comparison of four option groups, at `sidecar.js:1689-1694`. The check is key-order sensitive, so a reordered payload discards a warm pi session. |
| S8 | medium | `git_auth.js:65-146` hand-writes a shell tokenizer, used at `:169-208` to decide whether a command deserves a credential broker. One string is interpreted by two parsers with different grammars. The credential handling itself is well designed and should be preserved. |
| S9 | medium | The connection read buffer at `sidecar.js:1116-1120` is unbounded, so a peer that never sends a newline grows it until the process fails. This is a local defect only, because the socket is 0600 same-user. Contrast the bounded desktop reader at `sidecarSupervisor.ts:128-131`. |
| S10 | medium | The terminal path at `sidecar.js:1217-1252` and `:976-1045` is four shallow layers deep, and the middle two add three lines each. Six of 32 methods are normalise-and-delegate wrappers. |
| S11 | medium | `useAgentStream.ts:35-222` conjoins queueing with cancellation, and it also branches between two transports. `startPrompt` calls itself from its own finally block with no depth bound. |
| S12 | medium | `Session.tsx:190-391` mixes an 85-line slash-command switch, model resolution, and a 45-line catalogue sort. The builtin command list is stated three times, including at `ChatView.tsx:34-40`. |
| S13 | medium | Errors are flattened to fixed strings on every desktop error path. 32 throw sites in `main.ts` discard the cause, and desktop holds 187 in total, or roughly one per 17 lines. The renderer cannot distinguish a network failure from a rejected credential. |
| S14 | low | Four routes at `api/sessions.py:113-184` fetch a row, then run an owner check that issues a second three-table join at `api/deps.py:66-103`. That is two queries where one suffices, repeated eight times. |
| S15 | low | `tenant_db_encryption_required` at `config.py:244-266` returns false when the mode is enabled. There are four config values and only two function names. |
| S16 | low | Dead code. `WSPrompt` at `models.py:237` and `WSCancel` at `:253` are referenced by tests only, and `UserOut` at `:268` has no references. |
| S17 | low | Dead code. `showMenu` at `ChatView.tsx:78` is written four times and never read, and `streaming` at `:211` is listed as a dependency and unused in the body. |
| S18 | low | `optionById` at `CloudRunnerSection.tsx:172-178` throws on an unknown id. No caller can supply one, so the throw is unreachable and unhandleable. |
| S19 | low | A MiniMax pricing table sits at `services/keys.py:25-30`, inside a generic key-management module. It belongs in `model_catalog.py`. |
| S20 | low | `waitForCallback` at `hostedAuth.ts:37` declares an `expectedState` parameter that no implementation honours, and the adapter at `main.ts:331` drops it. See also `authCallbackListener.ts:11`. State validation happens later, at `hostedAuth.ts:150-155`. |
| S21 | low | Importing `main.py` builds middleware at lines 489-490 and can raise from `_validate_settings`. `prompt_journal.py:64` uses a lazy import to avoid this. |

## What is already good

These should survive any restructuring.

`services/crypto.py:47-243` holds one HKDF derivation and two envelope formats.
The interface is much simpler than the implementation.
`services/control_encryption.py` correctly stays a thin policy layer over it
instead of duplicating primitives.

`crypto/noiseIk.ts` hides all Noise state machine detail behind a six-member
interface at `:36-43`. Key material is zeroed on every path, at `:151`, `:344`,
`encryptedRunnerClient.ts:397`, and `encryptedUpload.ts:139-140`.

`desktop/src/secureJsonStore.ts:100-170` writes to a temporary file and syncs
both file and directory. It asserts permissions before and after, and reads with
`O_NOFOLLOW`. Three methods.

`desktop/src/accountLease.ts:84-159` is pure and total, with no dependencies
outside itself. Every failure path funnels through one `invalidLease()` helper,
which keeps the error path short.

`desktop/src/credentialStore.ts:160-262` serialises vault mutations through one
`#enqueue` field. Version-1 records migrate to version 2 during validation at
`:97-105`, which is the right home for the migration.

`services/desktop_auth.py:125-402` is genuinely deep.
`exchange_desktop_authorization_code` takes three parameters. Behind them it
hides PKCE verification, single-use code consumption, device creation, and token
issuance.

`services/workspace_runtime_paths.py:57-78` is a correct deep abstraction. One
call repairs the checkout, validates both paths against tenant storage, and
installs guardrails. Three of four callers use it.

`runtime/runtimeRef.ts:29-54` round-trips base64url and rejects non-canonical
encodings by re-encoding and comparing at `:50`.

`config.py:287-348` fails closed at startup on dangerous combinations, such as
no-auth mode bound to a non-loopback host.

`main.py:63-105` handles both the declared content length and the streamed case
in its body-limit middleware, which is commonly missed.

Dependency injection is applied consistently and without ceremony across the
frontend and desktop. See `RuntimeTransportDependencies`,
`RunnerClientDependencies`, `RuntimeResolverDependencies`, and the
`DesktopAppController` contract.

Comments generally explain reasoning instead of restating code.
`sidecar_runtime.py:437-441` explains why the repo is mounted at its host path.
That placement lets git worktree pointers resolve. `sidecar.js:681-684` and
`:1751-1756` record reasoning that the code cannot express. Preserve these
through any decomposition.

The sidecar is the only fully decoupled component. It knows nothing about
runtimes, tenants, or Noise, so it would extract cleanly.

## Suggested order of work

Sequenced so that early items are cheap, close real defects, or remove code that
later items would otherwise have to move.

Stage 1, close live defects. B1 session eviction, then B2 through the
`DesktopAccount` merge in D1. Both are small and both fix user-visible failures.

Stage 2, pure deletion. L3 storage profile table, about 130 lines. S1 and S2
container wrappers, about 87 lines plus 7 call sites. S16 through S18 dead code.
P2 dead SSE path, after confirming it is unreachable, about 90 lines. This stage
removes roughly 400 lines and touches no behaviour.

Stage 3, single-home the small decisions. F1 runner usability predicate, which
closes a real inconsistency. L5 default-runtime rule. L6 remaining duplications,
covering secret rules, excluded directories, encrypted field list, launch config,
profile layout, and secret validation.

Stage 4, fix the inversions. A1 prompt journal, extracting
`services/prompt_turn.py`. This also starts F2.

Stage 5, restore layering. L2 and D2 database schema and SQLCipher
consolidation. F2 remaining `api/stream.py` extraction, with
`services/session_store.py` and `services/repo_store.py`. Then add the F6
frontend response mappers and named resource functions.

Stage 6, decompose the large files. F3 sidecar, in the five-step order given.
F4 `main.ts`. F5 frontend oversized components, after stages 3 and 5 have removed
the logic they currently inline.

Stage 7, generate the duplicated tables. L1 route table generation. L4 protocol
constant generation. L7 API type generation. This stage has the highest
coordination cost. It is best attempted once earlier stages have reduced the
number of hand-written call sites.

Renaming work from A3 and A4 can be folded into whichever stage touches the
affected files.

## Open questions

Tracing the code raised these. Each one needs a decision from someone with
product context.

- Is the RPC envelope `v: 2` at `encryptedRunnerClient.ts:483` intentional against `_REQUEST_KEYS_V1` at `runner_rpc.py:17`?
- Does the backend accept the `query` key that the frontend always sends? It is absent from `_REQUEST_KEYS_V1`.
- Is `api/terminals.py` still live, or superseded by `api/terminal_channels.py`? Both are registered at `main.py:395-396`.
- Is `client.ts:streamPrompt` still live, or superseded by `promptStream.ts`? The analysis in P2 suggests it is unreachable.
- Are the three session byte caps of 64 KiB, 256 KiB, and 128 MiB intentional per channel, or drift?