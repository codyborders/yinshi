# Task for reviewer

[Read from: /Users/user/projects/yinshi/plan.md, /Users/user/projects/yinshi/progress.md]

Perform a SOFTWARE DESIGN review (A Philosophy of Software Design lens) of the DESKTOP app at /Users/user/projects/yinshi/desktop/src/ and the SIDECAR at /Users/user/projects/yinshi/sidecar/src/. Read the actual source files. IGNORE desktop/dist, desktop/release, node_modules.

Focus on the red-flag checklist: shallow modules, information leakage, temporal decomposition, pass-through methods, overexposure, conjoined methods, repetition, vague names, comment problems, special-general mixture, nonobvious code, too many exceptions.

Desktop priorities: main.ts (713 lines), hostedApiGateway.ts (392), hostedAuth.ts (365), credentialStore.ts (262), sidecarSupervisor.ts, helperSupervisor.ts, appController.ts, accountLease.ts, accountSession.ts, hostedAccessSession.ts, runtimeLaunchConfig.ts, runtimeSecrets.ts, secureJsonStore.ts, preload.ts, desktopApi.ts.

Sidecar priority: sidecar/src/sidecar.js is 2021 lines in ONE file - analyse its internal structure, responsibilities, and how it should decompose. Also git_auth.js (505).

Specifically check:
1. desktop/src/main.ts: what responsibilities does it hold? Is it an orchestrator or a dumping ground? Enumerate.
2. Session/auth/lease concept sprawl: accountSession.ts vs accountLease.ts vs hostedAccessSession.ts vs hostedAuth.ts vs credentialStore.ts vs runtimeSecrets.ts. Are these distinct concepts or one concept split by operation order (temporal decomposition)? This is the key question.
3. Supervisor duplication: sidecarSupervisor.ts vs helperSupervisor.ts - shared process-lifecycle logic?
4. preload.ts / desktopApi.ts IPC surface: is it a deep API or a wide shallow bridge of many small calls?
5. sidecar.js: enumerate its responsibilities and propose a module decomposition.

Output a precise report with FILE:LINE citations for every finding, severity (high/med/low), and a concrete suggested restructuring. Do NOT modify any files. Be concrete and quantitative, not generic.

---
**Output:**
Write your findings to exactly this path: /tmp/yinshi-design/desktop-sidecar.md
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