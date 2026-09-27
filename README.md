# Yinshi

Yinshi is a browser and desktop coding environment for running Pi agents against Git repositories. Users import a GitHub repository or an approved local repository, work in an isolated workspace, chat with the agent, inspect files and terminals, and review results through Git. Execution can be local, hosted, managed on Fly Sprites, or on a user-owned BYOC runner.

## How It Works

Repository import and workspace creation occur in the selected execution location. Private GitHub access uses installation-scoped authorization. Created workspaces use Git worktrees; desktop also supports validated linked local repositories. Chat streams Pi events while durable prompt journals support reconnect without submitting the same run again.

Manual child threads can run in isolated child workspaces and return sealed results without automatically merging changes. Six optional delegation tools use the same backend lifecycle. Agent delegation defaults off, and the current release scope allows one level of children. Users supply model-provider access for the selected location; stored provider secrets use AES-256-GCM encryption.

## Data Protection Model

Yinshi uses a middle-ground security model rather than zero-knowledge hosting. Execution processes still handle plaintext. Configured protections include per-user keys, SQLCipher databases, encrypted sensitive control fields, narrow container mounts and HTTPS. Managed/BYOC browser traffic uses Noise-encrypted RPC through the control-plane relay. Worktrees and Pi session files need storage-level protection independently of database encryption. See [the threat model](docs/security/middle-ground-threat-model.md) for the trust boundary and operator duties.

## Architecture

The Python 3.11+ FastAPI backend separates hosted control routes from desktop/worker execution routes. SQLite control and tenant schemas track identity, repositories, sessions, prompt events, threads, runner authority and managed recovery. Pydantic validates boundaries; Authlib provides OAuth; `cryptography` handles keys and encryption; Uvicorn serves ASGI.

The frontend is React 18 with React Router, TypeScript, Tailwind CSS and Vite. Runtime adapters cover local/hosted HTTP and encrypted managed/BYOC RPC. Chat, workspace inspection, settings and thread-tree components share the selected runtime. Vitest and Playwright cover browser behavior.

The Node sidecar bridges Pi SDK sessions, provider authorization, Git credentials, PTYs and delegation tools over Unix sockets. Its Pi packages are pinned to `0.84.1`. Podman-backed execution uses narrow mounts; managed runners and the Electron desktop package have separate runtime owners. Desktop supervises a Python helper and sidecar and targets macOS 14+ arm64.

Managed Sprites have encrypted off-provider backup and recovery services. The scheduled source-loss workflow still intentionally fails while live staging integration is pending. Broker/root-launcher and workspace-replica modules provide separately gated foundations. They do not replace the normal execution path. Source presence does not establish a live deployment or an isolation guarantee.

SQLCipher support is optional at install time. Production deployments that set `TENANT_DB_ENCRYPTION=required` must install either `sqlcipher3` or `pysqlcipher3` in the backend environment.

## Development

Settings load `.env` from the process working directory. The example is at the repository root; preserve any existing `.env` and keep credentials out of Git. Explicit local no-auth development uses `DISABLE_AUTH=true` with `CONTAINER_ENABLED=false`; authenticated and managed modes have additional validation. The sidecar must be available through the configured runtime/socket.

```bash
# Repository-root backend entry points; configure the existing .env separately.
rtk proxy python3 -m venv backend/.venv
rtk proxy backend/.venv/bin/python -m pip install -r backend/requirements/dev.txt
rtk proxy backend/.venv/bin/python -m pip install --no-deps -e backend
rtk proxy backend/.venv/bin/python -m uvicorn yinshi.main:app --reload
```

CI runs backend pytest and static checks, frontend Vitest/typecheck/build, desktop tests and packaging checks, and Node sidecar tests/container builds. Playwright is configured separately. See the literate document for runtime prerequisites, configuration defaults, deployment assets and validation boundaries.

## Project Structure

```text
backend/
  src/yinshi/            Control/execution APIs, storage, runtime and lifecycle services
  tests/                 Python contract, regression and integration suites
frontend/
  src/                   React workbench, runtime transports and browser tests
  e2e/                   Isolated Playwright end-to-end tests
  public/                Static assets and published architecture document
desktop/
  src/                   Electron gateway, credentials, supervisors and tests
sidecar/
  src/                   Node.js Pi bridge and orchestration protocol
  tests/                 Sidecar regression and lifecycle tests
deploy/                  Sprite bootstrap and gated broker/launcher templates
docs/                    Security, deployment, operations and thread contracts
lit/
  yinshi.lit.md          Complete annotated application source and repository reference
```

## Documentation

The reference is [the literate source](lit/yinshi.lit.md), with [HTML](lit/yinshi.html) also served from `frontend/public/architecture.html`. It documents commit `5e51a03a`, includes 231 complete application source files, and inventories all remaining tracked files. Source code is unchanged by this documentation refresh.

Focused references cover [thread orchestration](docs/thread-orchestration.md), [managed recovery](docs/operations/managed-runtime-recovery.md), [runner storage](docs/deployment/runner-storage-options.md), [broker/launcher status](docs/broker-root-launcher.md), and [offline replica reconciliation](docs/workspace-os-isolation.md). Historical plans and earlier status notes may lag current implementation; the literate reference distinguishes implemented, gated, deferred and unverified work.

## License

Use it or don't.
