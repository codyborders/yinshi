import type { HelperReadyMessage } from "./helperProtocol.js";

const INSTANCE_NONCE_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

export interface BootstrapHelperSessionOptions {
  readonly ready: HelperReadyMessage;
  readonly fetch: (input: string | URL, init?: RequestInit) => Promise<Response>;
}

export async function bootstrapHelperSession(
  options: BootstrapHelperSessionOptions,
): Promise<string> {
  if (!Number.isInteger(options.ready.port) || options.ready.port < 1 || options.ready.port > 65_535) {
    throw new TypeError("helper bootstrap port is invalid");
  }
  if (!INSTANCE_NONCE_PATTERN.test(options.ready.instanceNonce)) {
    throw new TypeError("helper bootstrap nonce is invalid");
  }
  if (typeof options.fetch !== "function") {
    throw new TypeError("helper bootstrap fetch adapter is invalid");
  }

  const origin = `http://127.0.0.1:${options.ready.port}`;
  const bootstrapResponse = await options.fetch(`${origin}/desktop/bootstrap`, {
    method: "POST",
    headers: { "X-Yinshi-Bootstrap": options.ready.instanceNonce },
    redirect: "error",
    signal: AbortSignal.timeout(5_000),
  });
  if (bootstrapResponse.status !== 204) {
    throw new Error("desktop helper bootstrap was rejected");
  }

  const healthResponse = await options.fetch(`${origin}/health`, {
    redirect: "error",
    signal: AbortSignal.timeout(5_000),
  });
  if (healthResponse.status !== 200) {
    throw new Error("desktop helper session was not established");
  }
  let health: unknown;
  try {
    health = await healthResponse.json();
  } catch {
    throw new Error("desktop helper health response was invalid");
  }
  if (
    typeof health !== "object" ||
    health === null ||
    Array.isArray(health) ||
    (health as Record<string, unknown>).status !== "ok"
  ) {
    throw new Error("desktop helper health response was invalid");
  }
  return origin;
}
