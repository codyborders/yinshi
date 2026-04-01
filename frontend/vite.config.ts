/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { configDefaults } from "vitest/config";
import { execSync } from "child_process";

const gitCommitHash = execSync("git rev-parse --short HEAD").toString().trim();

export default defineConfig({
  define: {
    __GIT_COMMIT_HASH__: JSON.stringify(gitCommitHash),
  },
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom"],
          router: ["react-router-dom"],
          datadog: ["@datadog/browser-rum", "@datadog/browser-rum-react"],
          markdown: ["react-markdown", "remark-gfm"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    exclude: [...configDefaults.exclude, "e2e/**"],
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
      },
      "/auth": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
