import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";

const CALLBACK_PATH = "/auth/desktop/callback";
const REQUEST_TARGET_LENGTH_MAX = 2_048;

export interface AuthCallbackListener {
  readonly callbackUri: string;
  waitForCallback(): Promise<URL>;
  close(): Promise<void>;
}

export interface StartAuthCallbackListenerOptions {
  readonly timeoutMs: number;
}

function writeTextResponse(
  response: import("node:http").ServerResponse,
  statusCode: number,
  message: string,
): void {
  response.writeHead(statusCode, {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'",
    "Content-Type": "text/plain; charset=utf-8",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    Connection: "close",
  });
  response.end(message);
}

function closeServer(server: Server): Promise<void> {
  if (!server.listening) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    server.close((error) => {
      if (error !== undefined) {
        reject(error);
        return;
      }
      resolve();
    });
    server.closeIdleConnections();
  });
}

export async function startAuthCallbackListener(
  options: StartAuthCallbackListenerOptions,
): Promise<AuthCallbackListener> {
  if (!Number.isInteger(options.timeoutMs) || options.timeoutMs < 1) {
    throw new TypeError("timeoutMs must be a positive integer");
  }
  if (options.timeoutMs > 10 * 60 * 1_000) {
    throw new TypeError("timeoutMs must not exceed ten minutes");
  }

  let callbackOrigin: string | undefined;
  let settled = false;
  let resolveCallback: ((callback: URL) => void) | undefined;
  let rejectCallback: ((error: Error) => void) | undefined;
  const callbackPromise = new Promise<URL>((resolve, reject) => {
    resolveCallback = resolve;
    rejectCallback = reject;
  });
  void callbackPromise.catch(() => undefined);
  let timeout: NodeJS.Timeout | undefined;

  const server = createServer((request, response) => {
    if (settled) {
      writeTextResponse(response, 410, "Authentication callback is no longer active.");
      return;
    }
    if (request.method !== "GET") {
      writeTextResponse(response, 405, "Method not allowed.");
      return;
    }
    if (request.url === undefined || request.url.length > REQUEST_TARGET_LENGTH_MAX) {
      writeTextResponse(response, 414, "Authentication callback is invalid.");
      return;
    }
    if (callbackOrigin === undefined) {
      writeTextResponse(response, 503, "Authentication callback is not ready.");
      return;
    }

    let callback: URL;
    try {
      callback = new URL(request.url, callbackOrigin);
    } catch {
      writeTextResponse(response, 400, "Authentication callback is invalid.");
      return;
    }
    if (callback.origin !== callbackOrigin || callback.pathname !== CALLBACK_PATH) {
      writeTextResponse(response, 404, "Not found.");
      return;
    }
    const codes = callback.searchParams.getAll("code");
    const states = callback.searchParams.getAll("state");
    if (codes.length !== 1 || states.length !== 1 || !codes[0] || !states[0]) {
      writeTextResponse(response, 400, "Authentication callback is invalid.");
      return;
    }

    settled = true;
    if (timeout !== undefined) {
      clearTimeout(timeout);
    }
    writeTextResponse(
      response,
      200,
      "Authentication complete. You can close this window and return to Yinshi.",
    );
    resolveCallback?.(callback);
    void closeServer(server).catch(() => undefined);
  });

  await new Promise<void>((resolve, reject) => {
    const onError = (error: Error): void => {
      server.off("listening", onListening);
      reject(error);
    };
    const onListening = (): void => {
      server.off("error", onError);
      resolve();
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen({ host: "127.0.0.1", port: 0, exclusive: true });
  });
  const address = server.address();
  if (address === null || typeof address === "string") {
    await closeServer(server);
    throw new Error("desktop auth callback listener address is unavailable");
  }
  const callbackPort = (address as AddressInfo).port;
  if (!Number.isInteger(callbackPort) || callbackPort < 1 || callbackPort > 65_535) {
    await closeServer(server);
    throw new Error("desktop auth callback listener port is invalid");
  }
  callbackOrigin = `http://127.0.0.1:${callbackPort}`;
  const callbackUri = `${callbackOrigin}${CALLBACK_PATH}`;

  timeout = setTimeout(() => {
    if (settled) {
      return;
    }
    settled = true;
    rejectCallback?.(new Error("desktop auth callback timed out"));
    void closeServer(server).catch(() => undefined);
  }, options.timeoutMs);
  timeout.unref();

  let explicitClose: Promise<void> | undefined;
  return {
    callbackUri,
    waitForCallback(): Promise<URL> {
      return callbackPromise;
    },
    close(): Promise<void> {
      explicitClose ??= (async () => {
        if (timeout !== undefined) {
          clearTimeout(timeout);
        }
        if (!settled) {
          settled = true;
          rejectCallback?.(new Error("desktop auth callback listener closed"));
        }
        await closeServer(server);
      })();
      return explicitClose;
    },
  };
}
