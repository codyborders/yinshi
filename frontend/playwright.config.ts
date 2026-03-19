import { defineConfig } from "@playwright/test";

import {
  backendBaseUrl,
  backendEnv,
  backendPython,
  backendRoot,
  backendTmpDir,
  e2eTmpDir,
  frontendBaseUrl,
  frontendRoot,
} from "./e2e/helpers/config";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  use: {
    baseURL: frontendBaseUrl,
    trace: "on-first-retry",
  },
  webServer: [
    {
      command: "node ./e2e/mock-sidecar.mjs",
      cwd: frontendRoot,
      url: "http://127.0.0.1:9777",
      reuseExistingServer: false,
      env: {
        ...backendEnv,
        MOCK_SIDECAR_HEALTH_PORT: "9777",
      },
    },
    {
      command:
        `bash -lc 'mkdir -p "${backendTmpDir}" "$USER_DATA_DIR" && "${backendPython}" -m uvicorn yinshi.main:app --host 127.0.0.1 --port 8000'`,
      cwd: backendRoot,
      url: `${backendBaseUrl}/health`,
      reuseExistingServer: false,
      env: backendEnv,
    },
    {
      command: `bash -lc 'mkdir -p "${e2eTmpDir}" && npm run dev -- --host 127.0.0.1 --port 5173'`,
      cwd: frontendRoot,
      url: frontendBaseUrl,
      reuseExistingServer: false,
    },
  ],
});
