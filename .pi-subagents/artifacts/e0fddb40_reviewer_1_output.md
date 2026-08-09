# Frontend Software Design Review

A Philosophy of Software Design lens applied to `/Users/user/projects/yinshi/frontend/src`. The directories `frontend/dist` plus `node_modules` were excluded.

Method: full reads of the ten largest source files. Targeted greps covered all 56 non-test modules. `npx tsc --noEmit` was run and passes with no errors. No files were modified.

The task referenced `plan.md` plus `progress.md` at the repository root. Neither file exists. `docs/plans/` and `code_reviews/` were read instead for context.

## 1. Quantitative summary

| File | Lines | `useState` | `useEffect` | Components | Raw `/api/...` literals |
| --- | --- | --- | --- | --- | --- |
| `components/Sidebar.tsx` | 1203 | 26 | 7 | 5 | 13 |
| `pages/Settings.tsx` | 968 | 24 | 4 | 5 | 4 |
| `components/CloudRunnerSection.tsx` | 837 | 17 | 2 | 2 | 4 |
| `components/WorkspaceInspector.tsx` | 757 | 12 | 4 | 4 | 7 |
| `pages/Session.tsx` | 668 | 12 | 7 | 1 | 4 |
| `api/client.ts` | 579 | -- | -- | -- | 5 |
| `runner/encryptedRunnerClient.ts` | 555 | -- | -- | -- | 2 |
| `components/ChatView.tsx` | 400 | 5 | 1 | 1 | 0 |

Cross-cutting counts follow. The codebase carries 60 raw `/api/...` URL literals across 16 modules, of which 32 sit inside `components/` or `pages/`. The global `window.yinshiDesktop` is referenced 27 times across 9 non-test modules. The discriminant `RuntimeRef.location` is inspected 29 times across 9 non-test modules. Five modules each declare a private `RESOURCE_ID_PATTERN = /^[0-9a-f]{32}$/`. Three modules each implement base64url encoding. Four call sites fetch `GET /api/settings/runner` under four different usability rules. Three files each define their own `formatTimestamp`.

## 2. Findings

### F1. Sidebar.tsx is a god module carrying six responsibilities (severity: high)

`components/Sidebar.tsx` holds 26 `useState` calls plus 7 effects in one file. Its four components are `Sidebar` (`:166-498`), `RepoSection` (`:500-806`), `WorkspaceItem` (`:808-953`), and `ImportForm` (`:955-1203`).

The first responsibility is multi-runtime discovery with a fan-out fetch. `loadRepos` (`:188-259`) probes the hosted runtime at `:200-217`, then probes the BYOC runner at `:218-243`, then aggregates three repository lists. It also computes a per-location banner list held in `locationErrors` (`:185`).

The second responsibility is GitHub installation state plus OAuth callback handling. See `githubNoticeFromSearch` (`:97-129`), `loadGitHubInstallations` (`:261-281`), plus the URL-rewriting effect (`:283-308`).

The third responsibility is repository-level settings editing. `RepoSection` owns an AGENTS.md draft editor whose state sits at `:522-525`, whose handlers sit at `:589-628`, and whose 60-line textarea form sits at `:695-757`.

The fourth responsibility is workspace plus session lifecycle. `createBranch` (`:546-573`) creates a workspace, creates a session, then routes the user to it. `WorkspaceItem.openOrCreateSession` (`:845-873`) duplicates that create-then-route logic.

The fifth responsibility is application chrome. The theme toggle, the settings link, the logout button, plus an inlined gear SVG all sit at `:398-483`.

The sixth responsibility is repository import validation. `ImportForm.handleSubmit` (`:1008-1063`) classifies user input as a git URL, a local path, or GitHub shorthand. It then enforces runtime-specific import rules.

The consequence is that unrelated concerns share one 1203-line file. A GitHub change, an AGENTS.md change, or a runtime discovery change all land here. The name `Sidebar` describes a screen region rather than an information-hiding boundary.

Suggested restructuring. Extract `useRuntimeRepositories()` into `runtime/`, returning `{ repos, runtimes, locationErrors, loading, error, reload }`. That hook becomes the single home for runtime plus repo discovery, which also fixes F5. Extract `components/RepoSettingsForm.tsx` for the AGENTS.md editor by moving state `:522-525`, handlers `:589-628`, plus markup `:695-757`. Extract `hooks/useGitHubInstallations.ts` for the second responsibility, including `githubNoticeFromSearch`. Merge `createBranch` with `openOrCreateSession` into one `startSession(transport, runtime, workspaceId)` helper placed beside `runtimeResourceId`. The target is a `Sidebar.tsx` under 300 lines holding layout plus composition only.

### F2. Runner usability is decided in four places under three rules (severity: high, correctness risk)

The predicate is duplicated and has already diverged.

| Location | Rule |
| --- | --- |
| `runtime/resolveRuntime.ts:31-38` | runner exists, id matches, status is not revoked, key confirmed, key present |
| `pages/Settings.tsx:751-755` | key confirmed, key present, status is not revoked |
| `components/Sidebar.tsx:222` | key confirmed, key present. No status check. |
| `components/CloudRunnerSection.tsx:358, :378, :401` | key present, key confirmed. No status check. |

`Sidebar.tsx:222` omits the revoked check. A revoked runner therefore still yields a `byoc` entry in `availableRuntimes` (`:225-229`), and its repositories are still listed (`:232-243`). The user can then select that runtime inside `ImportForm` and attempt an import. During the same session, `Settings.tsx` would already have hidden the BYOC option.

This is information leakage in the classic sense. One design decision lives in four modules, and the copies no longer agree.

Suggested restructuring. Add `runtime/runnerIdentity.ts` exporting `loadPairedRunnerRuntime(): Promise<RuntimeRef | null>`. All four call sites consume it. `resolveRuntimeRef` uses the same helper plus an id-match assertion.

### F3. Settings.tsx ProviderCard mixes three auth mechanisms with a poll loop (severity: high)

`pages/Settings.tsx:66-511` is one component holding 15 `useState` calls (`:77-101`). It implements API key form state through `secret`, `label`, `config`, plus `missingRequiredField` (`:171-188`). It implements `api_key_with_config` secret-shape construction in `structuredSecret` (`:152-169`), which encodes a backend payload contract inside a render-time `useMemo`. It implements a full OAuth flow in `connectProvider` (`:212-250`), whose 600-iteration polling loop at `:227-241` waits one second per iteration. It implements manual OAuth callback paste handling in `submitOauthCallbackInput` (`:252-284`). It also implements connection deletion at `:286-299`.

`resetOauthFlowState` (`:103-112`) plus `applyOauthFlowState` (`:114-131`) exist only to keep 7 related state values consistent. That is the signal for one state object or one reducer.

There is an additional runtime risk. The loop at `:227-241` is not bound to an `AbortController` or to unmount. Only the `connecting` flag guards re-entry. After unmount, `setError` plus `applyOauthFlowState` still fire, and up to 600 requests still go out.

Suggested restructuring. Extract `hooks/useProviderOauthFlow(transport, providerId)` returning `{ state, start, submitManualInput, cancel }`, driving the poll loop with an `AbortController` cleaned up in `useEffect`. Collapse the 7 OAuth state values into one object. Extract `models/providerConnection.ts` to hold `buildInitialConfig`, `normalizeFieldValue`, secret construction, plus required-field validation. That module then owns the wire payload for `POST /api/settings/connections`. Split `ProviderCard` into `ApiKeyProviderForm` plus `OauthProviderForm`, since the `hasKeyForm` and `hasOauth` booleans at `:133-135` already mark the boundary.

### F4. CloudRunnerSection.tsx conflates four concerns (severity: med-high)

`components/CloudRunnerSection.tsx:250-732` holds 17 `useState` calls (`:251-273`). Seven async operations serve four different concerns. Provisioning covers `loadRunner` (`:275-294`), `createRunner` (`:301-335`), plus `revokeRunner` (`:436-451`). Noise identity pairing covers `confirmRunnerIdentity` (`:337-355`). Connectivity diagnostics covers `checkRunnerConnection` (`:357-375`). BYOC repository CRUD covers `loadRunnerRepositories` (`:377-398`), `importRepositoryToRunner` (`:400-434`), plus `RunnerRepositoryPanel` (`:734-837`).

The fourth concern is a repository browser embedded inside a settings section. It duplicates what `Sidebar.tsx:218-243` already does through `listRunnerRepositories`. The third concern is a single button that calls `checkEncryptedRunnerHealth`.

The static option catalogue at `:31-83` is 53 lines of copy plus warnings. It is display content interleaved with behaviour, and it belongs in its own module.

`optionById` (`:172-178`) throws on an unknown id. Its only caller is `:462` passing `selectedOption`. That value comes from `optionForRunner` (`:157-170`) or from a radio input restricted to `RUNNER_OPTIONS`. The throw is therefore unreachable, and no caller can handle it. Following "define errors out of existence", it should return a default or accept a narrower parameter type.

Suggested restructuring. Move `RUNNER_OPTIONS`, `STORAGE_LABELS`, `optionForRunner`, `optionById`, `runnerProfileValue`, plus `isRunnerStorageProfile` into `runner/runnerStorageOptions.ts`. Extract `hooks/useCloudRunner()` for the first three concerns. Move `RunnerRepositoryPanel` with its two handlers into a component file that consumes `runner/repositories.ts` directly.

### F5. Runtime selection is decided in at least six places (severity: high)

The intended single decision point is `runtime/useRuntimeResource.ts:29-79`. It parses the encoded id, resolves the ref, then builds the transport. `pages/Session.tsx:65-69` uses it correctly and is the model to follow.

Every other consumer re-derives the answer.

| Location | Independent decision |
| --- | --- |
| `components/Sidebar.tsx:172-174` | builds `defaultRuntime` from `window.yinshiDesktop` |
| `components/Sidebar.tsx:200-217` | decides that hosted probing is desktop-only |
| `components/Sidebar.tsx:269` | picks hosted or default for GitHub installs |
| `pages/Settings.tsx:604-606` | rebuilds `primaryRuntime` inside `RuntimeLocationSelector` |
| `pages/Settings.tsx:696-698` | rebuilds `primaryRuntime` again inside `Settings` |
| `api/client.ts:376, :408` | decides desktop-hosted routing by path membership |
| `runtime/runtimeTransport.ts:266, :331` | decides `hostedApi` against `api`, plus hosted upload routing |

`runtime/runtimeRef.ts:64-68` already exports `defaultRuntimeRef({ desktop })`, which is the correct home for that decision. Only `parseRuntimeResourceId` uses it today. Three sites reimplement it inline at `Sidebar.tsx:172`, `Settings.tsx:604`, plus `Settings.tsx:697`.

Suggested restructuring. Replace the three inline ternaries with `defaultRuntimeRef`, then add `runtime/environment.ts` exporting `isDesktop()`. That cuts 27 `window.yinshiDesktop` references down to roughly 8 genuine bridge call sites. Add `runtime/useAvailableRuntimes()` as the sole producer of the runtime list, consumed by both `Sidebar` and `Settings`. That change also fixes F2.

### F6. The BYOC scope table is duplicated across frontend and backend (severity: med)

`runtime/runtimeTransport.ts:96-206` holds `requiredScope`, which is a near line-for-line port of `_required_scope` at `backend/src/yinshi/services/runner_rpc.py:131-215`. The same 18 path regexes appear on both sides. Compare `runtimeTransport.ts:48-89` against the module-level patterns in `runner_rpc.py`.

Defence in depth justifies a client-side scope request. The two copies have already diverged. The backend handles four pi-upload path patterns at `runner_rpc.py:206-215`. The frontend table has no upload entries at all, so it would throw `"BYOC runtime method or path is not allowed"` for them. The upload flow therefore bypasses `createRuntimeTransport` entirely, as described in F7.

Adding one BYOC-reachable route now requires coordinated edits in two languages. No test asserts that the two tables agree.

Suggested restructuring. Generate both tables from one declarative route-scope manifest checked into the repository. A cheaper option is a contract test that enumerates the frontend table then asserts each entry against the backend table.

### F7. encryptedUpload bypasses the transport policy layer (severity: med)

`runtime/runtimeTransport.ts:322-341` special-cases `upload`. For `byoc` it calls `uploadEncryptedPiConfig`. For desktop-hosted it calls `uploadHostedPiConfig`. Both open their own connections.

`runtime/encryptedUpload.ts:150-186` calls `connectEncryptedRunner` directly, hard-coding `scopes: ["pi.configure"]` at `:159`. It skips `requiredScope`. It also skips `parseByocPath` plus `validateRuntime`.

The result is two independent BYOC request paths under three different session limits. `encryptedRunnerClient.ts:15` defaults to 64 KiB. `runtimeTransport.ts:90` uses 256 KiB. `encryptedUpload.ts:10` uses 128 MiB.

`runtimeTransport.ts:323-338` also hard-codes one allowed upload path, `"/api/settings/pi-config/upload"`. `encryptedUpload.ts:99-131` never sends that path. It sends `/api/settings/pi-config/uploads`, then `/uploads/{id}/chunks/{i}`, then `/uploads/{id}/complete`. The guard validates a path that is discarded immediately afterwards.

Suggested restructuring. Give `RuntimeTransport` an explicit `uploadPiConfig(file)` method in place of the generic `upload(path, file)`, so the path stops being a caller concern. Move the session-byte constants into one `runtime/limits.ts`.

### F8. api/client.ts is over-general and over-layered rather than shallow (severity: med)

`api/client.ts` is no one-function-per-endpoint wrapper. It exposes 6 generic verb methods on `api` (`:414-440`), 6 more on `hostedApi` (`:442-451`), plus `streamPrompt` (`:515-572`) with `cancelSession` (`:574-579`).

The 579 lines break down as follows. Wire type declarations occupy `:1-254`, covering 25 exported interfaces. Error normalisation occupies `:256-320`. Transport occupies `:322-451`. SSE handling occupies `:453-579`.

The abstraction value is real yet thin. The module centralises credentials, the CSRF header, the 401 redirect, error normalisation, plus 204 handling. `_readApiError` with `_normalizeErrorPayload` (`:256-320`) is genuinely deep work behind a simple interface.

Four problems remain, and each is the opposite of shallowness.

Pass-through layering. The same 6-method signature is declared four times: `JsonApiClient` (`runtimeTransport.ts:22-29`), `RuntimeTransport` (`runtimeTransport.ts:36-44`), `api` (`client.ts:414-440`), plus `hostedApi` (`client.ts:442-451`). `hostedApi.upload` at `:450` is a literal pass-through to `api.upload`. `hostedRequest` (`:403-411`) delegates with an unchanged signature.

Overexposure of the URL space. The interface is `get<T>(path)`, so every caller must know URL structure. 32 raw `/api/...` literals sit inside components or pages, and `Sidebar.tsx` alone holds 13.

Nonobvious desktop routing. `DESKTOP_HOSTED_RUNNER_PATHS` (`:317-321`) silently re-routes exactly three paths through the desktop bridge. A reader of `api.get("/api/settings/runner")` at `Sidebar.tsx:220` cannot see that. `request` (`:374-380`) also performs the check twice, once directly and once inside `desktopHostedRequest`, which is a conjoined-method smell.

Dead legacy streaming path. `streamPrompt` plus `cancelSession` are used only at `useAgentStream.ts:127` and `:193`. Those branches run when `runtimeTransport` is falsy. In `pages/Session.tsx:66-70`, `id` plus `transport` both derive from the same `runtimeResource` object, so they are undefined together. `sendPrompt` then returns early at `useAgentStream.ts:47`. The SSE branch is unreachable. That is roughly 90 dead lines, plus a branch that doubles the cost of reading the hook.

Suggested restructuring. Delete `streamPrompt`, `cancelSession`, plus the fallback branches at `useAgentStream.ts:120-134` and `:186-195`. Collapse `hostedApi` into `api`, or move `DESKTOP_HOSTED_RUNNER_PATHS` into `runtimeTransport.ts` beside the other routing decisions. Move the 254 lines of wire types into `api/wireTypes.ts` so `client.ts` reads as transport only. Add named resource functions such as `fetchRepos(transport)` plus `patchWorkspaceState(...)` so components stop carrying URL literals. That last change does the most to shrink `Sidebar.tsx` with `WorkspaceInspector.tsx`.

### F9. Backend wire shapes are used raw in components (severity: med)

`models/sessionModels.ts` maps only model refs plus thinking levels, across 169 lines and 9 exported functions. Everything else flows raw into the UI.

Snake_case wire-field accesses in presentation code, counted per file:

| File | Snake_case field accesses |
| --- | --- |
| `pages/Settings.tsx` | 24 |
| `components/PiReleaseNotesSection.tsx` | 19 |
| `components/Sidebar.tsx` | 17 |
| `components/CloudRunnerSection.tsx` | 16 |
| `components/PiConfigSection.tsx` | 10 |
| `components/ToolCallBlock.tsx` | 8 |
| `pages/Session.tsx` | 4 |

Concrete instances follow. `Sidebar.tsx:222-243` reads `noise_key_confirmed`, `noise_public_key`, plus `id`, then hand-builds a `RuntimeRef`. `Settings.tsx:751-760` repeats that construction, as covered in F2. `Sidebar.tsx:522`, `:592-596`, plus `:601` read and write `repo.agents_md`, and line `:593` encodes a backend null convention inside a JSX component. `CloudRunnerSection.tsx:137-144` reaches into `runner.capabilities[key]`, an untyped `Record<string, unknown>` from `client.ts:87`, then defends against non-string values inline at `:142`. `Session.tsx:116-135` maps `Message` rows into `ChatMessage` inline inside `loadHistory`, and line `:129` performs an unchecked `m.role` cast. `WorkspaceInspector.tsx:246-249` decodes a `{ content?, diff? }` union by branching on `mode`, so the component knows that two endpoints name their payload field differently.

The correct pattern already exists elsewhere. `runner/repositories.ts:9-24`, `runtime/promptStream.ts:67-124`, plus `runtime/terminalChannel.ts:46-59` all validate then map at the boundary. That discipline is simply absent for settings, runners, repos, and message history.

Suggested restructuring. Add `models/runnerModels.ts`, `models/repoModels.ts`, plus `models/messageModels.ts`. Move the `Message` to `ChatMessage` mapping out of `Session.tsx:116-135`. No component should touch a snake_case field.

### F10. Repetition of small utilities (severity: low-med, broad)

| Duplicated element | Locations |
| --- | --- |
| `RESOURCE_ID_PATTERN` | `encryptedUpload.ts:12`, `terminalChannel.ts:3`, `runtimeRef.ts:3`, `promptStream.ts:8`, plus `RESOURCE_ID` at `runtimeTransport.ts:48` |
| base64url encoding | `encryptedRunnerClient.ts:85-96`, `runtimeRef.ts:17-27`, `encryptedUpload.ts:22-26` |
| `formatTimestamp` | `Settings.tsx:22-27`, `CloudRunnerSection.tsx:113-118`, `PiConfigSection.tsx:22-27`. Bodies identical. Fallback strings differ. |
| Error-message extraction | `CloudRunnerSection.tsx:133-135`, `usePiConfig.ts:31-36`, plus 18 inline ternaries at `WorkspaceInspector.tsx:257,283`, `useCatalog.ts:29`, `usePiReleaseNotes.ts:26`, `useRuntimeResource.ts:71`, with 5 more sites in `Settings.tsx` |
| Pointer-drag resize | `Session.tsx:478-500` matches `WorkspaceInspector.tsx:624-648` structurally. Their readers `Session.tsx:56-63` and `WorkspaceInspector.tsx:77-84` differ only in storage key plus bounds. |
| Runtime identity string | `Sidebar.tsx:45-50`, `piCommandsCache.ts:20-23`, `runtimeRef.ts:70-85`. Three encodings of one idea, in three formats. |

Suggested restructuring. Create `utils/errors.ts`, `utils/format.ts`, `utils/base64url.ts`, plus `runtime/resourceId.ts`. Add `hooks/useDragSize({ storageKey, min, max, initial, axis })`. Export one `runtimeKey(runtime)` from `runtime/runtimeRef.ts`, then delete the copies.

### F11. Session.tsx mixes command dispatch with catalogue sorting and layout (severity: med)

`pages/Session.tsx:65-88` declares 12 state values in one component. Three concerns are separable.

The slash-command interpreter is `handleCommand` (`:258-343`), an 85-line switch over five commands. It inlines a blob-download implementation at `:314-330`.

Model resolution plus mutation is `updateSessionModel` (`:190-256`). It validates provider connections, resolves the model, sends a PATCH, then announces the result. The `announce: boolean` flag shows that two callers want different behaviour. `handleCommand` (`:280`) wants messages. `handleModelChange` (`:394-407`) does not, then adds its own message at `:403`.

Catalogue sorting for a select element is the 45-line `useMemo` at `:345-391`. It sorts models by connection status, then provider label, then model label. That is model-domain logic living inside a page.

The builtin command list is stated three times, at `ChatView.tsx:34-40`, in the `/help` text at `Session.tsx:266-274`, plus the switch arms at `Session.tsx:276-342`. Those three copies can drift apart.

Suggested restructuring. Move the sort into `models/sessionModels.ts` as `sortModelsForSelection(...)`. Move `handleCommand` into `session/slashCommands.ts` as one registry of `{ name, description, run }`, shared by `ChatView` plus `Session`. Split `updateSessionModel` into a pure mutation plus a caller-side announcement.

### F12. useAgentStream conjoins queueing, cancellation, plus two transports (severity: med)

See `hooks/useAgentStream.ts:35-215`. Three functions cannot be read independently. `sendPrompt` (`:200-222`) writes `queuedPromptRef` then calls `cancel` (`:180-198`). The `finally` block of `startPrompt` (`:161-177`) then re-invokes `startPrompt` for the queued prompt. Understanding one function requires reading the other two, which is the conjoined-methods flag.

The recursive re-entry at `:172-176` is also nonobvious. `startPrompt` calls itself from its own `finally` block with no depth bound. Repeated steering builds an implicit chain.

`upsertTurn` plus `scheduleUpsert` (`:70-108`) implement rAF-batched message coalescing inside the same closure. That is a separate, testable concern.

Suggested restructuring. Model the run lifecycle as an explicit reducer over `idle | running | stopping`, an enum that already exists at `:33`. Hold the queued prompt inside that state rather than in a ref read from a `finally` block. Extract the rAF batching into `utils/turnBuffer.ts`.

### F13. Dead and inert code (severity: low)

`components/ChatView.tsx:78` declares `showMenu`. It is written at `:146`, `:197`, `:208`, plus `:238`, and it is never read. `menuVisible` (`:141`) is derived from `slashFilter` with `filteredCommands`. Delete the state plus its four setters.

`components/ChatView.tsx:211` lists `streaming` as a `handleSubmit` dependency. The body at `:180-210` never references it.

`api/client.ts:515-579` plus the branches at `useAgentStream.ts:120-134` and `:186-195` are unreachable, as covered in F8. `runtime/runtimeTransport.ts:324-327` validates an upload path that is never requested, as covered in F7.

### F14. Comment and naming observations (severity: low)

Positive examples are `api/piCommandsCache.ts:6-8` with `:31-34`, `ChatView.tsx:8-11`, `:123-131`, `:159-162`, plus `Session.tsx:411-413`. Each records the reasoning behind a decision. Each preserves information the code cannot express.

Several comments restate the code. `Session.tsx:112` restates the function name below it. `Sidebar.tsx:552` restates the next four lines. `Settings.tsx:62-64` returns a 240-character user-facing paragraph from a function, which puts display copy in a code position.

Several names are vague. `LocatedRepo` (`Sidebar.tsx:29-32`) pairs a repo with its runtime, so `RepoWithRuntime` reads better. `locationErrors` (`:185`) holds sentences rather than error objects, so `unavailableRuntimeMessages` matches its content. `data` at `Sidebar.tsx:191` plus `:273` is broad, as are `runtimeState` with `runtimeResource` at `Session.tsx:66-67`. `runnerHealth` (`CloudRunnerSection.tsx:263`) is typed `string | null` and holds a success sentence, so `healthMessage` is accurate. `runnerStatusClass` (`CloudRunnerSection.tsx:120`) takes `string` then casts to `CloudRunnerStatus` at `:122`, and a narrower parameter type removes both the cast plus the fallback.

## 3. Direct answers to the five questions

God components. Confirmed for all three files. `Sidebar.tsx` holds 6 responsibilities across 26 `useState` calls, per F1. `Settings.tsx` holds 5 top-level responsibilities: account display, desktop profile management, FileVault status, runtime-location selection, plus tab routing. It also contains the `ProviderCard` sub-god with 15 `useState` calls, per F3. `CloudRunnerSection.tsx` holds 4 concerns across 17 `useState` calls, per F4.

Is `api/client.ts` deep or shallow. Neither, precisely. It exposes 14 public members plus 25 wire interfaces, and it is no per-endpoint wrapper. Its centralised credential handling, CSRF header, 401 redirect, plus error normalisation is genuine depth. Four failures undercut that. The 6-verb signature is redeclared four times across two files. The URL space is fully exposed, with 32 literals in UI code. A hidden path-membership rule re-routes three paths at `:317-321`. Roughly 90 lines of SSE code are unreachable. See F8.

Does `runtime/` leak transport details. Yes, although the abstraction itself is sound. `RuntimeTransport` (`runtimeTransport.ts:36-44`) is the right interface, and `useRuntimeResource` is the right entry point, as `Session.tsx` demonstrates. However the runtime choice is made in at least 6 places, per F5. `RuntimeRef.location` is inspected 29 times across 9 modules, and some of those inspections are presentation code that should not care. `WorkspaceInspector.tsx:321` hides the download link for `byoc`. `Sidebar.tsx:516` collapses BYOC repos by default. `promptStream.ts:174` picks a poll delay by location. `piCommandsCache.ts:22` keys its cache by location. The first two are UI policy driven by a transport detail, and both should read a capability flag on the transport instead.

Duplication across `encryptedRunnerClient.ts`, `crypto/noiseIk.ts`, plus `runtimeTransport.ts`. The layering here is correct and is the best-factored part of the codebase. `noiseIk.ts` owns Noise IK framing and knows nothing about HTTP. `encryptedRunnerClient.ts` owns capability issuance, the relay handshake, plus RPC framing. `runtimeTransport.ts` owns path-to-scope policy. No overlapping responsibility was found. The real duplications are narrower. Base64url encoding exists three times. Session-byte limits exist three times with three values, per F7. `encryptedUpload.ts` bypasses the policy layer entirely, also per F7. The larger duplication is cross-language, between `requiredScope` plus the backend `_required_scope`, per F6.

Data-shape leakage. Backend wire formats are used raw in components. `models/sessionModels.ts` covers only model refs plus thinking levels. 98 snake_case wire-field accesses were counted across 7 UI files, per F9. Validated boundary mapping works well in `runner/repositories.ts`, `runtime/promptStream.ts`, plus `runtime/terminalChannel.ts`. It is absent for repos, workspaces, sessions, messages, runners, provider connections, and pi config.

## 4. Suggested remediation order

Start with F2 by unifying the runner usability predicate. It is the smallest change, and it removes a real behavioural inconsistency at `Sidebar.tsx:222`.

Next take F13 together with the dead SSE path from F8. Deleting `showMenu`, `streamPrompt`, `cancelSession`, plus the dead branches is pure subtraction of roughly 120 lines. It also halves the branching inside `useAgentStream`.

Third, address F5. Add `runtime/environment.ts`, route all default-runtime construction through `defaultRuntimeRef`, then add `useAvailableRuntimes()`.

Fourth, address F9 alongside the resource functions from F8. Adding `models/*` mappers plus named resource functions is what actually shrinks `Sidebar.tsx` with `WorkspaceInspector.tsx`.

Fifth, split the three god components from F1, F3, plus F4 along the boundaries described above. Do this after steps three and four remove the shared logic those components inline today.

Sixth, consolidate the small utilities from F10 as the earlier extractions touch them.

Last, unify the scope table source plus the encrypted upload path from F6 with F7. This carries the highest coordination cost and the lowest urgency, though the divergence is already visible.

## 5. What is already good

`runtime/promptStream.ts:67-124` plus `runtime/terminalChannel.ts:46-59` validate every response field before use. Both also check sequence contiguity, at `promptStream.ts:110-112` and `terminalChannel.ts:181-183`.

`crypto/noiseIk.ts` hides all Noise state machine detail behind a 6-member interface (`:36-43`). Key material is zeroed on every path, at `:151`, `:344`, `encryptedRunnerClient.ts:397`, plus `encryptedUpload.ts:139-140`.

`runtime/runtimeRef.ts:29-54` round-trips base64url and rejects non-canonical encodings by re-encoding then comparing at `:50`. That is a genuinely deep two-function interface.

Dependency injection is applied consistently and without ceremony. See `RuntimeTransportDependencies` (`runtimeTransport.ts:31-34`), `RunnerClientDependencies` (`encryptedRunnerClient.ts:60-72`), plus `RuntimeResolverDependencies` (`resolveRuntime.ts:12-14`).

`api/piCommandsCache.ts` is small and well commented, with a clear invalidation contract plus per-runtime keying. `npx tsc --noEmit` passes cleanly, and 20 test files cover hooks, runtime modules, crypto, plus the larger components.