# Task for reviewer

[Read from: /Users/user/projects/yinshi/plan.md, /Users/user/projects/yinshi/progress.md]

Perform a SOFTWARE DESIGN review (A Philosophy of Software Design lens) of the FRONTEND at /Users/user/projects/yinshi/frontend/src/. Read the actual source files. IGNORE frontend/dist and node_modules.

Focus on the red-flag checklist: shallow modules, information leakage, temporal decomposition, pass-through methods, overexposure, conjoined methods, repetition, vague names, comment problems, special-general mixture, nonobvious code, too many exceptions.

Prioritise: components/Sidebar.tsx (1203 lines), pages/Settings.tsx (968), components/CloudRunnerSection.tsx (837), components/WorkspaceInspector.tsx (757), pages/Session.tsx (668), api/client.ts (579), runner/encryptedRunnerClient.ts (555), components/ChatView.tsx, runtime/*.ts, hooks/*.

Specifically check:
1. God components: how much state and unrelated responsibility sits in Sidebar.tsx, Settings.tsx, CloudRunnerSection.tsx? List the distinct responsibilities each holds.
2. Is api/client.ts a deep module or a shallow one-function-per-endpoint wrapper? Count methods vs. abstraction value.
3. Does the runtime/ directory (resolveRuntime, runtimeRef, runtimeTransport, promptStream, terminalChannel, encryptedUpload) leak transport details into components/hooks? Is 'which runtime am I talking to' decided in one place or many?
4. Duplication between runner/encryptedRunnerClient.ts, crypto/noiseIk.ts and runtime/runtimeTransport.ts.
5. Data-shape leakage: are backend wire formats used raw in components, or mapped through models/sessionModels.ts?

Output a precise report with FILE:LINE citations for every finding, severity (high/med/low), and a concrete suggested restructuring. Do NOT modify any files. Be concrete and quantitative, not generic.

---
**Output:**
Write your findings to exactly this path: /tmp/yinshi-design/frontend.md
This path is authoritative for this run.
Ignore any other output filename or output path mentioned elsewhere, including output destinations in the base agent prompt, system prompt, or task instructions.

## Acceptance Contract
Acceptance level: attested
Completion is not accepted from prose alone. End with a structured acceptance report.

Criteria:
- criterion-1: Return concrete findings with file paths and severity when applicable

Required evidence: review-findings, residual-risks

Finish with a fenced JSON block tagged `acceptance-report` in this shape:
Use empty arrays when no items apply; array fields contain strings unless object entries are shown.
`criteriaSatisfied[].status` must be exactly one of: satisfied, not-satisfied, not-applicable.
`commandsRun[].result` must be exactly one of: passed, failed, not-run.
`manualNotes` and `notes` are optional strings; an empty string means no note and does not satisfy `manual-notes` evidence.
```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "specific proof"
    }
  ],
  "changedFiles": [
    "src/file.ts"
  ],
  "testsAddedOrUpdated": [
    "test/file.test.ts"
  ],
  "commandsRun": [
    {
      "command": "command",
      "result": "passed",
      "summary": "short result"
    }
  ],
  "validationOutput": [
    "validation output or concise summary"
  ],
  "residualRisks": [
    "none"
  ],
  "noStagedFiles": true,
  "diffSummary": "short description of the diff",
  "reviewFindings": [
    "blocker: file.ts:12 - issue found, or no blockers"
  ],
  "manualNotes": "anything else the parent should know"
}
```