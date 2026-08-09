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
        manualChunks(moduleId) {
          if (moduleId.includes("node_modules/react-router")) return "router";
          if (moduleId.includes("node_modules/react")) return "react";
          if (moduleId.includes("node_modules/@datadog")) return "datadog";
          if (
            moduleId.includes("node_modules/react-markdown") ||
            moduleId.includes("node_modules/remark")
          ) {
            return "markdown";
          }
          return undefined;
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
      "/rum": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
