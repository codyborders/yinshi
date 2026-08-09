# Task for scout

Map the CROSS-CUTTING architecture of /Users/user/projects/yinshi. Four components: backend/src/yinshi (Python FastAPI), frontend/src (React), desktop/src (Electron), sidecar/src (Node). IGNORE all of: backend/.venv, node_modules, dist, release, .git.

Answer these questions with FILE:LINE citations:

1. CONCEPT DUPLICATION ACROSS LANGUAGE BOUNDARIES: The Noise IK / encryption protocol appears in backend (services/runner_noise.py, runner_noise_session.py, control_encryption.py, crypto.py) and frontend (crypto/noiseIk.ts, runner/encryptedRunnerClient.ts) and desktop (runtimeSecrets.ts, diskEncryption.ts). Is there ONE authoritative definition of the wire protocol/framing, or is it re-derived independently in each place? Where would a protocol change require coordinated edits? List every file that would need to change.

2. TYPE/SCHEMA DUPLICATION: Are API request/response shapes defined once (e.g. generated from OpenAPI) or hand-written in both backend/src/yinshi/models.py and frontend TypeScript? List the duplicated shapes.

3. RUNTIME ABSTRACTION: The system appears to support multiple execution 'runtimes' (hosted/cloud runner, local desktop, sidecar container). Where is the decision of 'which runtime' made? Enumerate every location that branches on runtime kind in ALL four components. This is the key finding I need.

4. TERMINAL / STREAMING: trace the terminal and agent-stream data path end to end across all four components, naming each hop file. Note where the same framing/parsing logic is reimplemented.

5. Build a compressed component-dependency map: what depends on what, and note any circular or surprising dependencies.

Output a dense factual report. Do NOT modify files. Cite FILE:LINE everywhere.

---
**Output:**
Write your findings to exactly this path: /tmp/yinshi-design/cross-cutting.md
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