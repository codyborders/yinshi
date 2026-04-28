import path from "node:path";
import { fileURLToPath } from "node:url";

const helpersDir = path.dirname(fileURLToPath(import.meta.url));

export const frontendRoot = path.resolve(helpersDir, "..", "..");
export const workspaceRoot = path.resolve(frontendRoot, "..");
export const backendRoot = path.join(workspaceRoot, "backend");
export const e2eTmpDir = path.join(frontendRoot, "e2e", ".tmp");
export const backendTmpDir = path.join(e2eTmpDir, "backend");
export const repoBaseDir = path.join(e2eTmpDir, "repos");
export const socketPath = path.join(e2eTmpDir, "mock-sidecar.sock");
export const backendBaseUrl = "http://127.0.0.1:8000";
export const frontendBaseUrl = "http://127.0.0.1:5173";
export const backendPython = path.join(workspaceRoot, ".venv", "bin", "python");
export const authCookieScript = path.join(frontendRoot, "e2e", "helpers", "auth_cookie.py");

export const backendEnv = {
  ...process.env,
  PYTHONPATH: path.join(backendRoot, "src"),
  DB_PATH: path.join(backendTmpDir, "legacy.db"),
  CONTROL_DB_PATH: path.join(backendTmpDir, "control.db"),
  USER_DATA_DIR: path.join(backendTmpDir, "users"),
  ENCRYPTION_PEPPER: "a".repeat(64),
  SECRET_KEY: "playwright-secret-key",
  GOOGLE_CLIENT_ID: "fake-client-id",
  GOOGLE_CLIENT_SECRET: "fake-secret",
  DISABLE_AUTH: "false",
  ALLOWED_REPO_BASE: repoBaseDir,
  CONTAINER_ENABLED: "false",
  SIDECAR_SOCKET_PATH: socketPath,
  FRONTEND_URL: frontendBaseUrl,
  DEBUG: "true",
};
