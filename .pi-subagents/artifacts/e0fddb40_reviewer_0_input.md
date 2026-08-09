# Task for reviewer

[Read from: /Users/user/projects/yinshi/plan.md, /Users/user/projects/yinshi/progress.md]

Perform a SOFTWARE DESIGN review (A Philosophy of Software Design lens) of the BACKEND at /Users/user/projects/yinshi/backend/src/yinshi/. Read the actual source files. IGNORE backend/.venv entirely.

Focus on the red-flag checklist: shallow modules, information leakage, temporal decomposition, pass-through methods, overexposure, conjoined methods, repetition, vague names, comment problems, special-general mixture, nonobvious code, too many exceptions.

Prioritise the largest/most central files: api/stream.py, api/auth_routes.py, services/pi_config.py, services/container.py, db.py, services/runners.py, models.py, runner_agent.py, services/sidecar_runtime.py, services/workspace_files.py, services/terminal_journal.py, services/prompt_journal.py, main.py, tenant.py, config.py, services/container.py, and the api/ layer as a whole.

Specifically check:
1. Is the api/ layer a thin pass-through over services/, or does it hold business logic? Quantify.
2. Do services/ modules leak persistence/SQL details to callers? Look at db.py and how services use it.
3. Is there a coherent layering (api -> services -> db) or do layers skip?
4. Duplicated crypto/noise/relay logic across runner_noise.py, runner_noise_session.py, runner_relay.py, runner_agent_relay.py, runner_rpc.py, control_encryption.py, crypto.py, keys.py.
5. Duplicated runtime-resolution logic across desktop_runtime.py, worker_runtime.py, sidecar_runtime.py, git_runtime.py, workspace_runtime_paths.py.

Output a precise report with FILE:LINE citations for every finding, severity (high/med/low), and a concrete suggested restructuring. Do NOT modify any files. Be concrete and quantitative, not generic.

---
**Output:**
Write your findings to exactly this path: /tmp/yinshi-design/backend.md
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