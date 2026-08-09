# Software Design Review: Desktop App and Sidecar

Scope: `desktop/src/**` and `sidecar/src/**`.
Desktop has 25 non-test modules and 3,167 non-test lines.
Sidecar has 5 modules and 2,584 lines.
Lens: the red-flag checklist from "A Philosophy of Software Design".
Method: full read of every prioritised file.
Cross-checks: `backend/src/yinshi/api/auth_routes.py`, `backend/src/yinshi/services/desktop_auth.py`, and the pi SDK `BashOperations` contract.
No files were modified.

## Size and test baseline

| Module | Lines | Test file | Note |
| --- | --- | --- | --- |
| `sidecar/src/sidecar.js` | 2,021 | none direct | 1 class, 32 methods, 13 responsibilities |
| `desktop/src/main.ts` | 713 | none | one 342-line function |
| `desktop/src/hostedApiGateway.ts` | 392 | yes | 180-line hand-written route table |
| `desktop/src/hostedAuth.ts` | 365 | yes | |
| `sidecar/src/git_auth.js` | 505 | yes | hand-written shell tokenizer |
| `desktop/src/credentialStore.ts` | 262 | yes | |
| `desktop/src/secureJsonStore.ts` | 170 | none | covered only through `credentialStore.test.ts` |

Desktop sources with no test file: `main.ts` (713), `secureJsonStore.ts` (170), `signInRenderer.ts` (62), `desktopApi.ts` (51), `preload.ts` (30).

The two largest untested files also hold the most responsibilities.
That correlation is not an accident.
Both mix policy with process handles and window handles, so both resist testing.

---

## Part 1: main.ts responsibilities

Verdict: `main.ts` is a dumping ground with an orchestrator hidden inside it.
`DesktopAppController` at `desktop/src/appController.ts:36-149` is the real orchestrator.
That module is clean.
`main.ts` is the residue left after the extraction stopped halfway.

### Finding D1: main.ts holds thirteen responsibilities (severity: high)

1. Shell environment curation at `main.ts:67-83`. It picks inherited variables and the fallback `PATH`.
2. Profile directory naming and creation at `main.ts:85-105`. It owns the SHA-256 profile identifier and the five-directory layout.
3. Development executable discovery at `main.ts:107-128`. It searches `PATH` for `node` and `git`.
4. Development launch configuration at `main.ts:130-163`. This is a second implementation of `runtimeLaunchConfig.ts`.
5. Git executable resolution at `main.ts:165-171`.
6. Sign-in asset path and shell policy construction at `main.ts:173-186`.
7. Window security event wiring at `main.ts:188-208`.
8. Window creation and lifetime at `main.ts:210-235` and `main.ts:665-680`.
9. IPC sender authorisation policy at `main.ts:237-264`. It decides which page may call which channel.
10. Dependency composition at `main.ts:266-403`. This includes a 36-line inline sign-in flow at `main.ts:310-345`.
11. Eight IPC handlers at `main.ts:405-605`.
12. Process startup and update policy at `main.ts:609-654`. This includes update-ready dialog copy.
13. Application lifecycle events at `main.ts:655-713`.

Item 11 hides a full business flow.
The local repository import handler at `main.ts:502-605` is 104 lines.
It performs directory selection, clone, dirty-repository confirmation, and a second clone attempt.
It then calls the local helper over HTTP.
It validates the response shape and deletes the clone on failure.

`configureApplication` spans `main.ts:266-607`, which is 342 lines in one function.
This is not a composition root.
Composition roots do not hold rollback logic (`main.ts:466-481`) or dialog copy (`main.ts:548-560`).

Restructuring, ordered by payoff:

- Add `profileLayout.ts`. Move `profileDirectoryPath` and `ensureProfileDirectories` from `main.ts:85-105`. Place them beside the layout knowledge in `runtimeLaunchConfig.ts:104-160`. Export one function `profileLayout(userDataPath, userId)`. This also fixes D10.
- Add `repositoryImportFlow.ts`. Move `main.ts:502-605` behind one function. Inject `chooseDirectory`, `confirmDirtyImport`, `registerRepository`, and `gitCommand`. The flow then becomes testable without Electron.
- Add `ipcRouter.ts`. Move `requestFromAllowedPage` (`main.ts:237-264`) and the eight registrations. Each handler repeats a four-line guard and a rewrap. That is about 30 duplicated lines.
- Add `signInFlow.ts`. Move `main.ts:310-345` into one function. It owns the listener lifetime and the stage log.
- Fold `developmentLaunchConfig` (`main.ts:130-163`) into `buildRuntimeLaunchConfig`. Add a `mode` parameter. See D9.

Target size for `main.ts`: under 150 lines.
It should hold app-event wiring and one composition call.

---

## Part 2: session, auth, and lease concept sprawl

This is the key question, so the answer is given in full.

### What each module actually owns

| Module | Real responsibility | Depth |
| --- | --- | --- |
| `accountLease.ts` (159) | Ed25519 lease verification and signing-key pinning. No I/O. | Deep. Good module. |
| `credentialStore.ts` (262) | Multi-profile vault, active selection, persistence. | Deep. Good module. |
| `secureJsonStore.ts` (170) | Encrypted atomic file with permission checks. | Deep. Good module. |
| `hostedAuth.ts` (365) | Hosted sign-in protocol and token-response parsing. | Deep. |
| `accountSession.ts` (125) | One function. It refreshes, or falls back to the offline lease. | Shallow to medium. |
| `hostedAccessSession.ts` (99) | In-memory access-token cache with refresh de-duplication. | Shallow. |

### Finding D2: accountSession and hostedAccessSession are one concept split by operation order (severity: high)

This is textbook temporal decomposition.
`resumeDesktopAccount` covers startup and token expiry.
`HostedAccessSession` covers the time between those moments.
Neither module owns the account.
Both manipulate the same three facts.
Those are the active profile, the access token, and the expiry.

The split shows up in the wiring.
`main.ts:285-292` builds `HostedAccessSession` with a `resume` callback into `resumeDesktopAccount`.
`main.ts:301-309` builds a second closure.
That closure calls `resumeDesktopAccount` and pushes the result back in.
The same operation appears twice, in two directions, inside 25 lines.

The cost is not only readability.
It is a correctness hazard:

- `HostedAccessSession.getAccessToken` (`hostedAccessSession.ts:57-84`) de-duplicates refreshes through `#refresh`. It guards stale writes through `#epoch`.
- `DesktopAppController.switchProfile` (`appController.ts:110-121`) calls `resumeAccount`. That path reaches `resumeDesktopAccount` through `main.ts:301-309`. It skips `#refresh`.
- `resumeDesktopAccount` posts the stored refresh token at `accountSession.ts:88-99`.
- The backend rotates that token and records the old hash at `backend/src/yinshi/services/desktop_auth.py:270-276`.
- A replayed token revokes the whole device at `backend/src/yinshi/services/desktop_auth.py:244-255`.

Two concurrent refreshes therefore do more than waste a request.
The second refresh looks like token replay.
The backend revokes the device, and the user must sign in again.
The window is reachable.
A renderer `hostedRequest` refresh (`main.ts:295-298`) can overlap a `switchProfile` call (`main.ts:450-481`).

Restructuring: collapse the two modules into one owner.
Keep the pure parts separate.

```ts
// desktopAccount.ts replaces accountSession.ts and hostedAccessSession.ts
export class DesktopAccount {
constructor(options: { apiBaseUrl: string; fetch: FetchAdapter; credentialStore: DesktopCredentialStore; now?: () => number });
get profile(): DesktopCredentialProfile | undefined;
resume(): Promise<DesktopAccountState>;
accessToken(): Promise<string>;
adopt(session: HostedDesktopSession): void;
clear(): Promise<void>;
}
```

All four operations share one internal queue.
Every path that spends the stored refresh token then waits on the same promise.
`verifyAccountLease` stays in `accountLease.ts`.
`startHostedSignIn` and `readHostedDesktopTokenResponse` stay in `hostedAuth.ts`.
The offline-lease fallback becomes private state of `DesktopAccount`.
See `accountSession.ts:50-70` and `accountSession.ts:100-106`.
Offline eligibility is an account question, not a transport question.

Net effect: two modules and two closures become one module and one field.
The replay hazard is defined out of existence instead of defended against.

### Finding D3: runtimeSecrets is named as if it joined the auth cluster (severity: low)

`RuntimeSecretStore` at `runtimeSecrets.ts:48-89` has no link to the user account.
It generates three local encryption keys for the Python helper.
It shares only the `SecureJsonStore` mechanism.
The name places it beside `credentialStore` and `accountSession` in the reader's mind.
Suggested rename: `helperKeyStore.ts` with `HelperKeyStore`.
The values are consumed only as helper environment variables at `runtimeLaunchConfig.ts:140-151`.

### Finding D4: runtime-secret validation has two homes (severity: medium)

`validateRuntimeSecrets` exists twice with identical regular expressions.
See `runtimeSecrets.ts:20-46` and `runtimeLaunchConfig.ts:72-82`.
The second copy exists because `buildRuntimeLaunchConfig` does not trust its caller.
Export the validator once and import it.
A branded `RuntimeSecrets` type produced only by the store also works.

---

## Part 3: supervisor duplication

### Finding D5: the two supervisors duplicate the process-lifecycle pattern (severity: medium)

| Concern | Sidecar | Helper |
| --- | --- | --- |
| Liveness check | `sidecarSupervisor.ts:73`, `196-198` | `helperSupervisor.ts:24-26`, `124-126` |
| Wait for exit with timeout | `sidecarSupervisor.ts:71-86` | `helperSupervisor.ts:28-42` |
| SIGTERM, wait, SIGKILL, wait, throw | `sidecarSupervisor.ts:88-100` | `helperSupervisor.ts:44-59` |
| Spawn, await readiness, kill on failure | `sidecarSupervisor.ts:154-186` | `helperSupervisor.ts:99-121` |
| Memoised `stop()` | `sidecarSupervisor.ts:189-203` | `helperSupervisor.ts:127-131` |

That is about 60 lines of parallel logic.
The copies have already drifted in three ways:

1. Ordering. `helperSupervisor.ts:48-49` registers the exit listener before SIGTERM. `sidecarSupervisor.ts:95-96` sends SIGTERM first. Both are correct today, because `waitForExit` re-checks `exitCode`. A reader must verify that twice.
2. Option shape. `SidecarOptions.args` is `readonly string[]` at `sidecarSupervisor.ts:9`. `StartManagedHelperOptions.arguments` is `string[]` at `helperSupervisor.ts:17`. One concept, two names, two mutabilities. `localRuntime.ts:44-52` pays for that by copying arrays.
3. Error vocabulary. Compare `sidecarSupervisor.ts:99` with `helperSupervisor.ts:57`.

Restructuring: extract `childProcessLifecycle.ts`.

```ts
export interface ChildLifecycleOptions<Ready> {
readonly command: string;
readonly args: readonly string[];
readonly environment: Readonly<Record<string, string>>;
readonly workingDirectory?: string;
readonly stdio: StdioOptions;
readonly startupTimeoutMs: number;
readonly shutdownTimeoutMs: number;
readonly awaitReadiness: (child: ChildProcess, signal: AbortSignal) => Promise<Ready>;
readonly onStarted?: () => Promise<void>;
readonly onStopped?: () => Promise<void>;
}
export function startSupervisedChild<Ready>(o: ChildLifecycleOptions<Ready>): Promise<Supervised<Ready>>;
```

`onStarted` carries the sidecar socket permission check.
`onStopped` carries the sidecar socket removal.
`sidecarSupervisor.ts` then keeps only sidecar-specific parts.
Those are stale socket removal (`sidecarSupervisor.ts:43-58`), the permission assertion (`sidecarSupervisor.ts:60-70`), and the readiness reader (`sidecarSupervisor.ts:102-152`).
`helperSupervisor.ts` keeps only the descriptor-3 reader at `helperSupervisor.ts:61-88`.
Estimated size change: 343 lines become about 230.

A second option deserves weighing.
The two readiness mechanisms are themselves inconsistent.
The sidecar prints `SOCKET_PATH=...` on stdout.
The helper writes JSON on file descriptor 3.
One shared descriptor-3 message (`helperProtocol.ts:11-48`) would remove the strategy parameter.
That is a cross-process protocol change, so it belongs in a separate change.

### Finding D6: localRuntime is a thin pass-through with one real idea (severity: low)

`startLocalRuntime` at `localRuntime.ts:19-98` forwards `ready` and `processId` at `localRuntime.ts:66-67`.
Its real value is the combined `running` getter (`localRuntime.ts:68-70`) and the ordered shutdown (`localRuntime.ts:71-96`).
The socket-path cross-check at `localRuntime.ts:22-29` is a symptom.
It re-verifies an invariant that `buildRuntimeLaunchConfig` already guarantees.
The check exists because `main.ts:130-163` can break that invariant.
After D9 lands, the check becomes dead defence.

---

## Part 4: the IPC surface

### Finding D7: the bridge is thin, but one channel carries the weight (severity: low for the bridge, high for the channel)

`preload.ts:9-27` exposes eight methods.
Each is a one-line `ipcRenderer.invoke`.
As a set this is not a harmful wide bridge.
Seven of the eight are distinct user intents.
None is an accessor that leaks internal state.
Each hides a substantial main-process operation.
`desktopApi.ts` holds shared channel names and payload types, so preload and main cannot drift.

Two real defects here:

1. `DesktopProfileSummary` is declared twice. See `credentialStore.ts:22-26` and `desktopApi.ts:29-33`. The fields are identical, and the types have no structural link. `main.ts:444-449` returns the first through a channel typed with the second. Fix by importing one from the other.
2. `fileVaultStatus` at `desktopApi.ts:41` is a pure accessor for a rare display value. Fold it into a wider status payload if more such values appear. It is not urgent at one instance.

### Finding D8: hostedApiGateway mirrors the backend router by hand (severity: high)

`hostedApiGateway.ts:12-192` is a 180-line route table with 22 inline regular expressions.
It restates the hosted URL space inside the desktop main process.
The backend declares 81 routes under `backend/src/yinshi/api/`.
Every backend route change needs a matching edit here.
A miss fails closed at runtime with the message at `hostedApiGateway.ts:238`.

This is information leakage in the strict sense.
One decision, the hosted URL space, has two homes in two languages.

The table is not gratuitous.
It limits what a compromised renderer can reach.
So deletion is the wrong fix.
Options and tradeoffs:

- Generate the table at build time from the backend OpenAPI document into `hostedRoutes.generated.ts`. This keeps the control and removes hand-mirroring. Divergence becomes a build failure, not a runtime rejection. This is the recommended option.
- Reduce to a coarse policy over `/api/**` and `/auth/providers/**`. Use method rules and identifier-shape rules. This removes about 150 lines, but it weakens the boundary. Choose it only after deciding that the renderer is trusted.

Either way, state the query rules once.
They are currently duplicated at `hostedApiGateway.ts:44-66` and `hostedApiGateway.ts:180-191`.

### Finding D9: development and packaged launch configuration are duplicated (severity: medium)

`main.ts:130-163` rebuilds the helper and sidecar launch records for development.
It restates knowledge owned by `runtimeLaunchConfig.ts`.
Compare the argument shape at `main.ts:143-149` with `runtimeLaunchConfig.ts:165-169`.
Compare the sidecar entry point at `main.ts:154` with `runtimeLaunchConfig.ts:176`.
A helper argument change needs edits in both places.
Only the packaged path has tests, in `runtimeLaunchConfig.test.ts`.

Fix: add `mode` and `projectRoot` to `RuntimeLaunchConfigOptions`.
Resolve `command`, `workingDirectory`, and the asset directory inside `buildRuntimeLaunchConfig`.
`main.ts` then supplies paths, not structure.
This removes 34 lines from `main.ts`.
It also brings the development path under test.

### Finding D10: the profile layout is stated in three places (severity: medium)

The five subdirectories are created at `main.ts:93-105`.
They are consumed as environment variables at `runtimeLaunchConfig.ts:104-152`.
They are re-derived for the import flow at `main.ts:524-527`.
Adding a directory means editing two files and trusting the third.
Fix through the `profileLayout` function described in D1.

### Finding D11: waitForCallback declares an unused parameter (severity: low)

`hostedAuth.ts:37` declares `waitForCallback: (expectedState: string) => Promise<URL>`.
The listener implements `waitForCallback(): Promise<URL>` at `authCallbackListener.ts:11`.
The adapter at `main.ts:331` drops the argument.
State validation happens later, inside `validateCallback` at `hostedAuth.ts:150-155`.
The interface implies a contract that no implementation honours.
Fix: drop the parameter from the type.
Central state validation in `hostedAuth.ts` is correct.

### Finding D12: errors are flattened to fixed strings at every boundary (severity: medium)

`main.ts` has 32 throw sites.
Most discard the cause and throw a fixed string.
See `main.ts:322`, `343`, `412`, `421`, `435`, `470`, `477`, `480`, `565`, `587`, and `603`.
The stage value is written only to the console at `main.ts:342`.
The renderer cannot separate a network failure from a rejected credential.
It also cannot separate either one from a failed helper start.
Support logs face the same limit.

The desktop total is 187 throw sites across 3,167 non-test lines.
That is about one per 17 lines.
Many are internal argument checks with only in-package callers.
See `appController.ts:19-33`, `hostedAccessSession.ts:19-22`, and `hostedApiGateway.ts:277-284`.
In typed code with no external callers, these add cost without matching benefit.
They also make the error path longer than the normal path.

Recommendation: keep runtime validation at the two true trust boundaries.
Those are IPC input (`main.ts:405-605`) and anything parsed from disk or network.
Replace internal dependency assertions with types.
Add one `DesktopError` carrying a stable `code` so the renderer can branch.
Log the cause once, at the boundary.

---

## Part 5: sidecar.js responsibilities and decomposition

`YinshiSidecar` at `sidecar.js:932-2021` is one class of 1,090 lines.
It has 32 methods and three independent registries at `sidecar.js:934-936`.
Above it sit 900 lines of module-level helpers.
The class name is the first red flag.
"The sidecar" is a process, not a concept.

### Finding S1: thirteen responsibilities in one file (severity: high)

| # | Responsibility | Lines | Approximate size |
| --- | --- | --- | --- |
| 1 | Socket server, chmod, health-log timer | 933-944, 1085-1111, 1978-2020 | 90 |
| 2 | Newline framing and connection state | 1112-1146 | 35 |
| 3 | Request dispatch over 20 message types | 1147-1215 | 69 |
| 4 | Terminal registry and PTY lifetime | 45-57, 99-156, 976-1084, 1217-1252 | 250 |
| 5 | Pi session creation and reconciliation | 1554-1673 | 120 |
| 6 | Prompt execution and pi-event translation | 1675-1963 | 289 |
| 7 | Model catalog and reference resolution | 28-44, 269-302, 525-570, 838-887, 1254-1270, 1307-1329 | 180 |
| 8 | Provider auth normalisation | 446-524, 889-931, 1330-1358 | 140 |
| 9 | OAuth flow registry and manual-input relay | 34-35, 765-836, 1359-1553 | 260 |
| 10 | Pi resource listing with mtime cache | 607-763, 1287-1306 | 175 |
| 11 | Extension UI adapter and passthrough theme | 304-445 | 142 |
| 12 | Installed-package version lookup | 572-606, 1271-1286 | 50 |
| 13 | dotenv loading and settings normalisation | 158-236, 945-975 | 110 |

Responsibilities 4, 9, and 6 share no state and no logic.
They share only a socket.

### Finding S2: processQuery is 289 lines with a five-flag finalisation machine (severity: high)

`processQuery` spans `sidecar.js:1675-1963`.
Internal structure:

- `1676-1683`: option defaulting.
- `1689-1701`: change detection that decides whether to rebuild the pi session.
- `1702-1740`: teardown and rebuild.
- `1750-1790`: the finalisation machine with five mutable flags. Those are `agentEndEmitted`, `resultSent`, `pendingResult`, `compactionActive`, and `finalizeTimer`.
- `1791-1929`: a 139-line translation switch over ten pi event types.
- `1930-1942`: prompt execution and the synthetic-result workaround.
- `1943-1962`: error handling that separates cancellation from failure.

The last part is a conjoined-methods flag.
The catch block reads `entry.cancelRequested`, which `cancelSession` writes at `sidecar.js:1970`.
`cancelSession` depends on `processQuery` clearing that flag.
See `sidecar.js:1932` and `sidecar.js:1954`.
Neither method can be understood alone.

The finalisation machine is the least obvious code in either codebase.
`schedulePendingResult` at `sidecar.js:1778-1789` defers the result with a zero-millisecond timer.
That lets a `compaction_start` in the same turn cancel the pending result.
The comment at `sidecar.js:1751-1756` explains the inline-command half.
Nothing explains the zero-delay timer, which is the surprising part.

Restructuring: two extractions, both pure and both testable.

```js
// pi/eventTranslator.js is pure. A pi event goes in. Wire messages come out.
export function translatePiEvent(event, context);

// pi/runFinalizer.js owns the five flags and the deferral timer.
export function createRunFinalizer({ emit, buildResult });
// returned surface: onEvent(event), onPromptResolved(), onPromptRejected(error)
```

`processQuery` then drops to about 60 lines.
It resolves the session, subscribes the translator, runs the prompt, and reports the outcome.
The synthetic-result workaround at `sidecar.js:1936-1942` moves into `onPromptResolved`.
It can then be tested without a live model.

### Finding S3: activeSessions has no removal path (severity: high)

`activeSessions` is written at `sidecar.js:1655` and `sidecar.js:1739`.
It is read at `sidecar.js:1624`, `1683`, and `1966`.
It is cleared only in `cleanup()` at `sidecar.js:2005`, which runs at process exit.
There is no delete, no time-to-live, and no size cap.
Socket close only detaches terminals at `sidecar.js:1129-1132`.
The protocol has no dispose message.
`backend/src/yinshi/services/sidecar.py` never sends one.

Each retained entry holds a live pi session.
That includes model context, a settings manager, and a session file handle.
See `SessionManager.open` at `sidecar.js:205-235`.
In a long-running desktop process, every session opened stays resident until quit.

The inconsistency is instructive.
OAuth flows have a 30-minute time-to-live and a cap of 8.
See `sidecar.js:34-35` and `sidecar.js:1359-1378`.
Terminals are removed on exit at `sidecar.js:1017-1020`.
Only pi sessions have neither policy.

Fix: move the map into `pi/sessionRegistry.js`.
Add a maximum entry count and an idle time-to-live.
On eviction, call `unsubscribe()` and `piSession.dispose()`.
Add a `session_release` message so the backend can release deterministically.

### Finding S4: the wire field `id` carries four meanings (severity: medium)

Inside `handleRequest` at `sidecar.js:1147-1215`, `id` is:

- a pi session identifier for `query`, `warmup`, and `cancel`. See `sidecar.js:1201`, `1211`, and `1153`.
- a terminal identifier for `terminal_input`, `terminal_resize`, and `terminal_kill`. See `sidecar.js:1227`, `1236`, and `1245`.
- a workspace identifier fallback for `terminal_attach` at `sidecar.js:1218`. There `options.workspaceId || id` allows either.
- a request correlation identifier for `catalog`, `version`, `list_resources`, `resolve`, and the OAuth messages.

The correlation case is routinely replaced by a literal default.
See `sidecar.js:1257` and `sidecar.js:1385`.
The vague name forced eleven `id || "something"` expressions.
Fix in the protocol module.
Name the fields `sessionId`, `terminalId`, and `requestId`.
Require `requestId` on every request, so no handler needs a fallback literal.

### Finding S5: the terminal path is four shallow layers deep (severity: medium)

`handleTerminalAttach` (`sidecar.js:1217-1224`) calls `attachTerminal` (`1035-1045`).
That calls `terminalEntry` (`1024-1033`), which calls `createTerminalEntry` (`976-1022`).
The middle two layers add three lines each.
`handleTerminalInput` (`1226-1233`), `handleTerminalResize` (`1235-1242`), and `handleTerminalKill` (`1244-1252`) are normalise-and-delegate wrappers.
Six of 32 methods have that shape.

Fix: move the registry into `terminals/terminalRegistry.js`.
Expose `attach`, `write`, `resize`, and `kill`.
Each accepts the raw wire value and normalises it.
The dispatch table then calls the registry directly, and one layer disappears.

### Finding S6: session reuse is decided by JSON.stringify comparison (severity: medium)

`sidecar.js:1689-1694` compares four option groups by stringifying both sides.
Those are `providerAuth`, `providerConfig`, `gitAuth`, and `settings`.
The comparison is key-order sensitive.
A different key order from the backend tears down the pi session and rebuilds it.
The warm context is then lost for no reason.
The code is also non-obvious, because nothing states that the check is structural.

Fix: compute one `sessionFingerprint(options)` inside the session registry.
Derive it from an explicit ordered list of rebuild-forcing fields.
Name it, comment it once, and test it.

### Finding S7: the same error wrapper appears eleven times (severity: medium)

See `sidecar.js:1221`, `1230`, `1239`, `1249`, `1262`, `1278`, `1295`, `1321`, `1350`, `1468`, and `1527`.
Each handler ends with the same catch, format, and send shape.
That is about 90 duplicated lines.

Fix: let the dispatcher own it.
Use a table of type and handler pairs plus one wrapper that catches and sends.
This removes all eleven copies.
It also guarantees the message shape that `backend/src/yinshi/services/sidecar.py:283-285` depends on.

### Finding S8: git_auth reimplements shell semantics for a security decision (severity: medium-high)

`tokenizeShellCommand` at `git_auth.js:65-146` is a hand-written shell tokenizer.
`parseGitCommandForRuntimeAuth` at `git_auth.js:169-208` uses it.
It decides whether a command is a remote git operation that deserves a credential broker.
On a parse failure or a non-match, the command goes to the real shell.
See `git_auth.js:487` and `git_auth.js:492`.

So one string is interpreted by two parsers with different grammars.
The tokenizer is conservative at `git_auth.js:130-136`.
It rejects `$`, `;`, `|`, `>`, `<`, and backticks.
That instinct is right.
The design concern is durability.
Any future relaxation changes which commands receive a one-time GitHub token.
The two interpreters can only be kept in agreement by hand.

The credential handling itself is well designed and should be preserved.
It uses a per-command capability on an unlinked descriptor at `git_auth.js:246-256`.
It compares capabilities in constant time at `git_auth.js:258-271`.
It releases the credential once at `git_auth.js:311-317`.
It uses a `0600` socket inside a `0700` temporary directory.
See `git_auth.js:210-222` and `git_auth.js:330`.

Options:

- Keep the tokenizer and isolate it. Move it to `shellCommandGrammar.js`. State that it is an allow-list recogniser, not a shell. Add table-driven reject tests. This is the lowest-risk option and is recommended now.
- Use the pi SDK `spawnHook` from `BashToolOptions`. It rewrites the environment for every command. It would remove the need to recognise git at all. The obstacle is real. The capability is deliberately passed on descriptor 3, not in the environment. `spawnHook` adjusts only command, working directory, and environment. Moving the capability into the environment exposes it to every descendant process. Do not adopt this without a separate threat assessment.

Smaller item: `execOptions` is optional at `git_auth.js:408`, `426`, `441`, `453`, and `465`.
It is required at `git_auth.js:438-439`.
The SDK contract makes `options` required, so the optional chaining is the wrong half.
Pick one and remove the ambiguity.

### Finding S9: the connection read buffer is unbounded (severity: medium)

`sidecar.js:1116-1120` appends chunks with no length ceiling.
A peer that never sends a newline grows the buffer until the process fails.
The socket is `0600` and same-user at `sidecar.js:1089`.
The impact is therefore a local defect, not an attack.
A bound is still worth adding.
The helper and the sidecar are separate processes.
A helper defect should not stop the sidecar.
Note the contrast: the desktop reader is bounded at `sidecarSupervisor.ts:5` and `sidecarSupervisor.ts:128-131`.

### Finding S10: the pi UI adapter is a special case inside a general file (severity: low)

`createPassthroughTheme` and `createWebUIContext` span `sidecar.js:304-445`, which is 142 lines.
They exist only to satisfy the pi extension interface.
They are well commented, and the comments explain reasons rather than mechanics.
They simply do not belong beside the socket server.
Move them to `pi/webUiContext.js`.

### Proposed sidecar decomposition

| New module | Source lines moved | Estimated size |
| --- | --- | --- |
| `transport/socketServer.js` | 933-944, 1085-1146, 1978-2020 | 130 |
| `transport/protocol.js` | new field names and request schema | 80 |
| `router.js` | 1147-1215 plus the shared error wrapper | 90 |
| `terminals/terminalRegistry.js` | 45-57, 99-156, 976-1084, 1217-1252 | 240 |
| `pi/sessionRegistry.js` | 1554-1673, 1965-1977, plus eviction | 170 |
| `pi/eventTranslator.js` | 1791-1929 | 140 |
| `pi/runFinalizer.js` | 1750-1790, 1930-1962 | 90 |
| `pi/webUiContext.js` | 304-445 | 145 |
| `models/catalog.js` | 525-570, 1254-1270 | 70 |
| `models/resolver.js` | 269-302, 838-887, 1307-1329 | 110 |
| `auth/providerAuth.js` | 446-524, 889-931, 1330-1358 | 150 |
| `auth/oauthFlows.js` | 765-836, 1359-1553 | 260 |
| `resources/listResources.js` | 607-763, 1287-1306 | 180 |
| `runtimeVersion.js` | 572-606, 1271-1286 | 55 |
| `config/env.js` | 158-236, 945-975 | 110 |

`sidecar.js` then becomes a composition file of about 60 lines.
It wires the registries into the router and the router into the socket server.

Suggested order of work:

1. Extract `pi/eventTranslator.js` and `pi/runFinalizer.js`. Highest complexity per line, no I/O, immediately testable.
2. Extract `pi/sessionRegistry.js` with eviction. This also fixes S3.
3. Extract `router.js` with the shared error wrapper. This removes 90 duplicated lines and enables the S4 rename.
4. Extract `auth/oauthFlows.js`. Tests already exist in `sidecar/tests/oauth-flow-limits.test.js`.
5. Extract `terminals/terminalRegistry.js`. Tests already exist in `sidecar/tests/terminal.test.js`.

---

## Part 6: what is already good

State these so that a refactor does not damage them.

- `secureJsonStore.ts:100-170` is a genuinely deep module. It writes to a temporary file and syncs file and directory. It asserts permissions before and after. Reads use `O_NOFOLLOW`. The interface is three methods.
- `accountLease.ts:84-159` is pure, total, and self-contained. Every failure path funnels through one `invalidLease()` helper, which keeps the error path short.
- `credentialStore.ts:160-262` serialises vault mutations through one `#enqueue` field at `credentialStore.ts:171-178`. Version-1 records migrate to version 2 during validation at `credentialStore.ts:97-105`. That is the right home for the migration.
- `appController.ts:36-149` is a small orchestrator with an explicit dependency contract and one operation queue. `main.ts` should follow this model.
- `shellPolicy.ts:33-84` reduces navigation policy to two predicates. It is covered by `security.test.ts` and `shellPolicy.test.ts`.
- Comments at `sidecar.js:681-684`, `sidecar.js:1751-1756`, and `sidecar.js:607-615` record reasoning that code cannot express. Keep them through the decomposition.

---

## Priority summary

| ID | Finding | Severity | Effort |
| --- | --- | --- | --- |
| D2 | Account concept split by operation order. Concurrent refresh can revoke the device. | high | medium |
| S3 | `activeSessions` never evicts. Pi sessions stay for the process lifetime. | high | small |
| S2 | `processQuery` is 289 lines with a five-flag finalisation machine. | high | medium |
| D1 | `main.ts` holds thirteen responsibilities in one 342-line function. | high | medium |
| D8 | `hostedApiGateway` mirrors 81 backend routes by hand. | high | medium |
| S1 | `sidecar.js` is thirteen responsibilities in one file. | high | large |
| S8 | `git_auth.js` reimplements shell semantics for a security decision. | medium-high | small to isolate |
| D5 | Two supervisors duplicate the process-lifecycle pattern. | medium | small |
| D9 | Development and packaged launch configuration are duplicated. | medium | small |
| D10 | Profile layout stated in three places. | medium | small |
| D12 | Errors flattened to fixed strings. 187 throw sites. | medium | medium |
| S4 | Wire field `id` carries four meanings. | medium | small |
| S5 | Terminal path is four shallow layers deep. | medium | small |
| S6 | Session reuse decided by `JSON.stringify`. | medium | small |
| S7 | Same error wrapper repeated eleven times. | medium | small |
| S9 | Unbounded socket read buffer. | medium | trivial |
| D4 | Runtime-secret validation duplicated. | medium | trivial |
| D3 | `runtimeSecrets` named as part of the auth cluster. | low | trivial |
| D6 | `localRuntime` invariant check becomes dead after D9. | low | trivial |
| D7 | `DesktopProfileSummary` declared twice. | low | trivial |
| D11 | `waitForCallback` declares an unused parameter. | low | trivial |
| S10 | Pi UI adapter lives in the socket-server file. | low | trivial |

Two items deserve first place regardless of the wider restructuring.
Both are small, and both close a live defect.
Those are S3, session eviction, and D2, one account owner with one refresh queue.