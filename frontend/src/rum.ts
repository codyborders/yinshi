import type { RumBeforeSend } from "@datadog/browser-rum";
import { reactPlugin } from "@datadog/browser-rum-react";

function normalizeRumViewUrl(rawUrl: string): string {
  if (typeof rawUrl !== "string" || rawUrl.length === 0) {
    return "/other";
  }

  let pathname: string;
  try {
    pathname = new URL(rawUrl, "https://yinshi.invalid").pathname;
  } catch {
    return "/other";
  }
  if (/^\/app\/session\/[^/]+\/?$/.test(pathname)) {
    return "/app/session/:sessionId";
  }
  return "/other";
}

const filterRumEvent: RumBeforeSend = (event) => {
  if (!("type" in event) || event.type !== "view") {
    return false;
  }
  if (event.usr !== undefined || event.account !== undefined) {
    return false;
  }
  if (event.context !== undefined && Object.keys(event.context).length > 0) {
    return false;
  }

  const normalizedPath = normalizeRumViewUrl(event.view.url);
  event.view.url = normalizedPath;
  event.view.name = normalizedPath;
  event.view.referrer = "";
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
    trackResources: false,
    trackUserInteractions: false,
    trackLongTasks: false,
    defaultPrivacyLevel: "mask" as const,
    beforeSend: filterRumEvent,
    proxy: (options: { path: string; parameters: string }) =>
      `/rum${options.path}?${options.parameters}`,
    plugins: [reactPlugin({ router: false })],
  };
}
