import type { RumBeforeSend } from "@datadog/browser-rum";
import { reactPlugin } from "@datadog/browser-rum-react";

const filterRumEvent: RumBeforeSend = (event) => {
  if (!("type" in event)) {
    return false;
  }
  if (event.type === "error") {
    return false;
  }
  return true;
};

export function createRumConfiguration(version: string) {
  if (typeof version !== "string" || version.trim().length === 0) {
    throw new Error("version must be a non-empty string");
  }

  return {
    applicationId: "6ca07893-ea15-4577-88cb-ef72b856ad3e",
    clientToken: "pubbe7e2760d9e429d5cda2d2eb49a408be", // gitleaks:allow -- public browser token
    site: "datadoghq.com",
    service: "yinshi",
    env: "prod",
    version,
    sessionSampleRate: 10,
    // Coding sessions render private source, prompts, and tool output. Replay
    // remains disabled even though the SDK package supports it.
    sessionReplaySampleRate: 0,
    trackResources: true,
    trackUserInteractions: false,
    trackLongTasks: true,
    defaultPrivacyLevel: "mask" as const,
    beforeSend: filterRumEvent,
    proxy: (options: { path: string; parameters: string }) =>
      `/rum${options.path}?${options.parameters}`,
    plugins: [reactPlugin({ router: false })],
  };
}
