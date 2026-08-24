import {
  type HistoryCacheStore,
  isCanonicalHistoryCacheUserId,
  processHistoryCacheRequest,
} from "./sessionHistoryCacheCore";

type Authenticate = () => Promise<string | null>;
type Fetcher = typeof fetch;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function authenticateSessionHistoryCacheUser(
  fetcher: Fetcher = fetch,
): Promise<string | null> {
  try {
    const response = await fetcher("/auth/me", {
      cache: "no-store",
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return null;
    const body: unknown = await response.json();
    if (
      !isRecord(body) ||
      body.authenticated !== true ||
      !isCanonicalHistoryCacheUserId(body.user_id)
    ) {
      return null;
    }
    return body.user_id;
  } catch {
    return null;
  }
}

export async function bindHistoryCachePort(
  port: MessagePort,
  store: HistoryCacheStore,
  authenticate: Authenticate = authenticateSessionHistoryCacheUser,
): Promise<void> {
  let authenticatedUserId: string | null = null;
  try {
    authenticatedUserId = await authenticate();
  } catch {
    authenticatedUserId = null;
  }
  if (!isCanonicalHistoryCacheUserId(authenticatedUserId)) {
    port.close();
    return;
  }
  port.onmessage = (message) => {
    if (
      !isRecord(message.data) ||
      message.data.userId !== authenticatedUserId
    ) {
      return;
    }
    const response = processHistoryCacheRequest(store, message.data);
    if (response !== null) port.postMessage(response);
  };
  port.start();
}
