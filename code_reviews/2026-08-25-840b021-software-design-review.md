# Software Design Review: `840b021`

| Field | Value |
| --- | --- |
| Date | 2026-08-25 |
| Commit | `840b02189f8eb9be6db859853d3f03ad4cf1e3e1` |
| Comparison | Parent commit through `840b021` |
| Scope | Mobile Files and Terminal access in session workspaces |
| Lens | Principles from *A Philosophy of Software Design* |
| Status | Complete |

## Summary

The commit makes Files and Terminal available as separate mobile workspace views. It keeps the combined desktop inspector and prevents file activity when only the terminal is shown.

The review found four material design issues. Two affect production vocabulary and ownership. One repeats a lifecycle rule. One makes browser checks and captures less trustworthy. No other material design findings remain within the commit scope.

## Findings

### Medium: Mobile workspace state has a Boolean-style name

`frontend/src/pages/Session.tsx` names the state `workspacePanelOpen`, but the value stores `"files"`, `"terminal"`, or `null`.

This name hides the state meaning. A reader can mistake it for a Boolean and add incorrect conditions.

Required correction: rename the state to `activeMobileWorkspaceView` and rename its setter to match.

### Medium: Workspace tool vocabulary is repeated across modules and controls

`frontend/src/pages/Session.tsx` declares its own tool union and repeats Files and Terminal button definitions in the session header and mobile overlay. `frontend/src/components/WorkspaceInspector.tsx` declares the related inspector view union separately.

Adding or renaming a tool therefore requires coordinated edits in several locations. The tool contract and display labels lack one owner.

Required correction: export a narrow workspace tool type and one descriptor list from the inspector module. Derive the broader inspector view type from that tool type. Render both mobile control groups from the shared descriptors.

### Low: File activity gating repeats the same view rule

`frontend/src/components/WorkspaceInspector.tsx` repeats `!active || view === "terminal"` in file refresh and polling paths.

A later view change could update one path but miss another. The lifecycle rule should have one precise predicate.

Required correction: derive one file-activity predicate and reuse it in refresh callbacks and polling lifecycle checks.

### Low: Browser viewport helper and capture timing hide intent

`frontend/e2e/navigation.spec.ts` passes an unused name to `assertInViewport`, hard-codes one viewport size, and captures the initial session before readiness checks.

The helper cannot safely validate another viewport. The early capture can show an empty page even when the test later passes.

Required correction: remove the unused argument, read the current viewport dimensions, and capture the initial session after all header controls are ready.

## Preserved requirements

Mobile keeps direct Files and Terminal actions. Every header control fits within a 390 by 844 viewport. Users can switch tools and return to chat.

Files mode does not connect the terminal. Terminal mode does not poll workspace files. The mobile overlay retains safe-area padding.

Desktop keeps one combined inspector across responsive transitions.

## Corrections

### Precise mobile state

`Session` now uses `activeMobileWorkspaceView` and `setActiveMobileWorkspaceView`. The state type is `WorkspaceTool | null`, so its name and valid values describe the same concept.

### One workspace tool vocabulary

`WorkspaceInspector` now exports one readonly descriptor tuple. `WorkspaceTool` derives from its key values. Both mobile control groups render from the same descriptors, so tool keys and labels have one production owner.

### One file-activity rule

`WorkspaceInspector` now derives `hasFileActivity` once. Initial refresh, changed-file refresh, focus refresh, and polling reuse that predicate. Terminal-only mode remains free from file requests.

### Accurate browser checks and captures

The Playwright viewport helper now reads the active viewport dimensions and has no unused parameter. The initial capture waits for both workspace actions. It also waits for the Model and Thinking controls. Every control must be visible and within bounds.

## Validation

Focused Session and WorkspaceInspector checks passed 39 tests. The complete frontend run passed 376 tests across 43 files. Playwright passed all four navigation tests.

TypeScript checking and the production build passed. The Git whitespace check also passed.

The test run emitted existing local-storage and Browserslist warnings. Authlib also reported a deprecation warning. Intentional chunk-error tests logged their expected exceptions. None caused a test or build failure.

## Final assessment

All four findings are corrected. Advisory review found no remaining blocker. Parent review found and corrected one final duplication between the tool type and descriptor list. No material software-design finding remains within the reviewed scope.
