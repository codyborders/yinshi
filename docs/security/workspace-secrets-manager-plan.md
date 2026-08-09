# Workspace Secrets Manager Plan

Yinshi's terminal MVP uses `.env` files with Git guardrails. The app writes repo-local excludes, installs a pre-commit block, and hides `.env*` from browser file APIs.

That protects the common accident: committing a secret from the web UI or from a normal `git commit`. It does not stop a user who has terminal access from reading the file, copying it elsewhere, or bypassing hooks. Managed secrets should become the default place for credentials.

The target system stores secrets in each user's encrypted tenant data, using the same envelope pattern as provider credentials. A secret can belong to a user, repo, or workspace. Each secret also declares its runtime target. Terminal-only secrets go to shells. Agent-only secrets go to agent turns. Shared secrets go to both. Managed secrets are injected into the runtime environment and are not written into the Git worktree by default.

Runtime injection belongs in workspace runtime resolution. The backend decrypts selected values, validates variable names, and passes an environment overlay to the sidecar. The sidecar applies that overlay to started processes without persisting values. When a user changes a secret, active workspace runtimes are marked stale. The next terminal restart or agent turn receives the new values. Hot reload can come later, with an explicit prompt, because replacing variables in a live shell can surprise users.

The UI should treat values as write-only after creation. Users can see a secret's name, scope, runtime target, last update time, and value-present flag. They cannot reveal the stored value from the browser. Audit records should capture actor, action, scope, target runtime, and timestamp only. Values stay out of audit storage.

Name validation should reject reserved variables such as `PATH`, `HOME`, `SHELL`, plus Yinshi internal names. Logs must redact values. Telemetry must do the same. Error messages should refer to the variable name only. Browser file endpoints should keep blocking `.env*` across preview, writes, diffs, and downloads even after managed secrets ship. The terminal can still create `.env` files manually, but product guidance should steer credentials into managed secrets.

Migration should land in a short sequence. First, add encrypted `workspace_secrets` storage in the tenant database. Next, add UI for create, update, delete, and scope changes. Then extend terminal and agent runtime resolution to include decrypted environment overlays. After that, add stale-runtime detection with a clear restart prompt. Finally, update documentation so `.env` is described as manual legacy mode and managed secrets are the recommended path.
