import { api, hostedApi } from "../api/client";
import {
  uploadEncryptedPiConfig,
  uploadHostedPiConfig,
} from "./encryptedUpload";
import {
  connectEncryptedRunner,
  requestEncryptedRunner,
  type EncryptedRunnerConnection,
  type EncryptedRunnerConnectionOptions,
  type EncryptedRunnerRequest,
} from "../runner/encryptedRunnerClient";

export type RuntimeRef =
  | { readonly location: "local" }
  | { readonly location: "hosted" }
  | {
      readonly location: "managed";
      readonly runnerPublicKey: string;
    }
  | {
      readonly location: "byoc";
      readonly runnerId: string;
      readonly runnerPublicKey: string;
    };

type RuntimeMethod = "DELETE" | "GET" | "PATCH" | "POST" | "PUT";

interface JsonApiClient {
  get<T>(path: string): Promise<T>;
  post<T>(path: string, body?: unknown): Promise<T>;
  patch<T>(path: string, body?: unknown): Promise<T>;
  put<T>(path: string, body?: unknown): Promise<T>;
  delete(path: string): Promise<void>;
  upload<T>(path: string, file: File): Promise<T>;
}

type EncryptedRequest = <T>(request: EncryptedRunnerRequest) => Promise<T>;
type ConnectEncrypted = (
  options: EncryptedRunnerConnectionOptions,
) => Promise<EncryptedRunnerConnection>;

interface RuntimeTransportDependencies {
  readonly apiClient: JsonApiClient;
  readonly encryptedRequest: EncryptedRequest;
  readonly connectEncrypted?: ConnectEncrypted;
  readonly now?: () => number;
}

export interface RuntimeTransport {
  readonly runtime: RuntimeRef;
  get<T>(path: string): Promise<T>;
  post<T>(path: string, body?: unknown): Promise<T>;
  patch<T>(path: string, body?: unknown): Promise<T>;
  put<T>(path: string, body?: unknown): Promise<T>;
  delete(path: string): Promise<void>;
  upload<T>(path: string, file: File): Promise<T>;
  close(): void;
}

const RESOURCE_ID = "[0-9a-f]{32}";
const REPOSITORY_MEMBER_PATH = new RegExp(`^/api/repos/${RESOURCE_ID}$`);
const WORKSPACE_COLLECTION_PATH = new RegExp(
  `^/api/repos/${RESOURCE_ID}/workspaces$`,
);
const WORKSPACE_MEMBER_PATH = new RegExp(`^/api/workspaces/${RESOURCE_ID}$`);
const SESSION_COLLECTION_PATH = new RegExp(
  `^/api/workspaces/${RESOURCE_ID}/sessions$`,
);
const SESSION_MEMBER_PATH = new RegExp(`^/api/sessions/${RESOURCE_ID}$`);
const SESSION_READ_PATH = new RegExp(
  `^/api/sessions/${RESOURCE_ID}/(?:messages|tree)$`,
);
const SESSION_HISTORY_PAGE_PATH = new RegExp(
  `^/api/sessions/${RESOURCE_ID}/messages/page$`,
);
const SESSION_HISTORY_FIELD_PATH = new RegExp(
  `^/api/sessions/${RESOURCE_ID}/messages/${RESOURCE_ID}/field$`,
);
const PROMPT_RUN_COLLECTION_PATH = new RegExp(
  `^/api/sessions/${RESOURCE_ID}/runs$`,
);
const PROMPT_RUN_ACTIVE_PATH = new RegExp(
  `^/api/sessions/${RESOURCE_ID}/runs/active$`,
);
const PROMPT_RUN_EVENTS_PATH = new RegExp(
  `^/api/sessions/${RESOURCE_ID}/runs/${RESOURCE_ID}/events/[0-9]{1,6}$`,
);
const PROMPT_RUN_CANCEL_PATH = new RegExp(
  `^/api/sessions/${RESOURCE_ID}/runs/${RESOURCE_ID}/cancel$`,
);
const WORKSPACE_FILE_READ_PATH = new RegExp(
  `^/api/workspaces/${RESOURCE_ID}/files/(?:changed|diff|preview|tree)$`,
);
const WORKSPACE_FILE_WRITE_PATH = new RegExp(
  `^/api/workspaces/${RESOURCE_ID}/files/content$`,
);
const TERMINAL_COLLECTION_PATH = new RegExp(
  `^/api/workspaces/${RESOURCE_ID}/terminals$`,
);
const TERMINAL_MEMBER_PATH = new RegExp(
  `^/api/workspaces/${RESOURCE_ID}/terminals/${RESOURCE_ID}$`,
);
const TERMINAL_ACTION_PATH = new RegExp(
  `^/api/workspaces/${RESOURCE_ID}/terminals/${RESOURCE_ID}/(?:input|resize|restart)$`,
);
const TERMINAL_EVENTS_PATH = new RegExp(
  `^/api/workspaces/${RESOURCE_ID}/terminals/${RESOURCE_ID}/events/[0-9]{1,10}$`,
);
const PROVIDER_CONNECTION_MEMBER_PATH = new RegExp(
  `^/api/settings/connections/${RESOURCE_ID}$`,
);
const PROVIDER_AUTH_START_PATH = /^\/auth\/providers\/[a-z0-9-]{1,64}\/start$/;
const PROVIDER_AUTH_CALLBACK_PATH =
  /^\/auth\/providers\/[a-z0-9-]{1,64}\/callback$/;
const RUNTIME_TRANSPORT_SESSION_BYTES = 16 * 1024 * 1024;
const MANAGED_HISTORY_CONNECTIONS = 4;
// Twenty-four worst-case bounded history exchanges stay below 12.7 MiB:
// each uses a <524,500-byte response envelope, <=561 ciphertext bytes, and a <1 KiB request.
// More than 4,000,000 bytes remain below the 16 MiB session limit.
const MANAGED_HISTORY_REQUESTS_PER_CONNECTION = 24;

function isBoundedSessionHistoryRequest(
  method: RuntimeMethod,
  path: string,
): boolean {
  return (
    method === "GET" &&
    (SESSION_HISTORY_PAGE_PATH.test(path) ||
      SESSION_HISTORY_FIELD_PATH.test(path))
  );
}

function managedConnectionLane(
  scope: string,
  method: RuntimeMethod,
  path: string,
  query: Readonly<Record<string, string>>,
): string {
  if (
    scope === "terminal" &&
    method === "GET" &&
    TERMINAL_EVENTS_PATH.test(path)
  ) {
    return "terminal:events";
  }
  if (
    scope === "session.read" &&
    isBoundedSessionHistoryRequest(method, path)
  ) {
    if (SESSION_HISTORY_PAGE_PATH.test(path)) {
      return "session.read:history:0";
    }
    const messageId = path.split("/")[5];
    const fieldOffset = query.name === "full_message" ? 1 : 0;
    const laneIndex =
      (Number.parseInt(messageId.slice(-1), 16) + fieldOffset) %
      MANAGED_HISTORY_CONNECTIONS;
    return `session.read:history:${laneIndex}`;
  }
  return scope;
}

function requiredScope(method: RuntimeMethod, path: string): string {
  const repositoryCollection = path === "/api/repos";
  const repositoryMember = REPOSITORY_MEMBER_PATH.test(path);
  if (method === "GET" && (repositoryCollection || repositoryMember)) {
    return "repository.read";
  }
  if (
    (method === "POST" || method === "PATCH" || method === "DELETE") &&
    (repositoryCollection || repositoryMember)
  ) {
    return "repository.write";
  }

  const workspaceCollection = WORKSPACE_COLLECTION_PATH.test(path);
  const workspaceMember = WORKSPACE_MEMBER_PATH.test(path);
  if (method === "GET" && workspaceCollection) {
    return "workspace.read";
  }
  if (method === "POST" && workspaceCollection) {
    return "workspace.write";
  }
  if ((method === "PATCH" || method === "DELETE") && workspaceMember) {
    return "workspace.write";
  }

  const sessionCollection = SESSION_COLLECTION_PATH.test(path);
  const sessionMember = SESSION_MEMBER_PATH.test(path);
  const sessionRead = SESSION_READ_PATH.test(path);
  const sessionHistoryPage = SESSION_HISTORY_PAGE_PATH.test(path);
  const sessionHistoryField = SESSION_HISTORY_FIELD_PATH.test(path);
  if (
    method === "GET" &&
    (sessionCollection ||
      sessionMember ||
      sessionRead ||
      sessionHistoryPage ||
      sessionHistoryField)
  ) {
    return "session.read";
  }
  if (method === "POST" && sessionCollection) {
    return "session.write";
  }
  if (method === "PATCH" && sessionMember) {
    return "session.write";
  }

  const promptRunCollection = PROMPT_RUN_COLLECTION_PATH.test(path);
  const promptRunActive = PROMPT_RUN_ACTIVE_PATH.test(path);
  const promptRunEvents = PROMPT_RUN_EVENTS_PATH.test(path);
  const promptRunCancel = PROMPT_RUN_CANCEL_PATH.test(path);
  if (method === "POST" && (promptRunCollection || promptRunCancel)) {
    return "session.stream";
  }
  if (method === "GET" && (promptRunActive || promptRunEvents)) {
    return "session.stream";
  }
  if (method === "GET" && WORKSPACE_FILE_READ_PATH.test(path)) {
    return "files.read";
  }
  if (method === "PUT" && WORKSPACE_FILE_WRITE_PATH.test(path)) {
    return "files.write";
  }
  const terminalCollection = TERMINAL_COLLECTION_PATH.test(path);
  const terminalMember = TERMINAL_MEMBER_PATH.test(path);
  const terminalAction = TERMINAL_ACTION_PATH.test(path);
  const terminalEvents = TERMINAL_EVENTS_PATH.test(path);
  if (method === "POST" && (terminalCollection || terminalAction)) {
    return "terminal";
  }
  if (method === "GET" && terminalEvents) {
    return "terminal";
  }
  if (method === "DELETE" && terminalMember) {
    return "terminal";
  }
  const providerCollection = path === "/api/settings/connections";
  const providerMember = PROVIDER_CONNECTION_MEMBER_PATH.test(path);
  if ((method === "GET" || method === "POST") && providerCollection) {
    return "provider.configure";
  }
  if (method === "DELETE" && providerMember) {
    return "provider.configure";
  }
  if (method === "GET" && path === "/api/catalog") {
    return "provider.configure";
  }
  if (method === "POST" && PROVIDER_AUTH_START_PATH.test(path)) {
    return "provider.configure";
  }
  if (
    (method === "GET" || method === "POST") &&
    PROVIDER_AUTH_CALLBACK_PATH.test(path)
  ) {
    return "provider.configure";
  }
  if (
    (method === "GET" || method === "DELETE") &&
    path === "/api/settings/pi-config"
  ) {
    return "pi.configure";
  }
  if (
    method === "GET" &&
    (path === "/api/settings/pi-config/commands" ||
      path === "/api/settings/pi-release-notes")
  ) {
    return "pi.configure";
  }
  if (
    method === "POST" &&
    (path === "/api/settings/pi-config/github" ||
      path === "/api/settings/pi-config/sync")
  ) {
    return "pi.configure";
  }
  if (method === "PATCH" && path === "/api/settings/pi-config/categories") {
    return "pi.configure";
  }
  throw new Error("Remote runtime method or path is not allowed");
}

function parseRemotePath(path: string): {
  readonly pathname: string;
  readonly query: Readonly<Record<string, string>>;
} {
  if (typeof path !== "string" || !path.startsWith("/") || path.includes("#")) {
    throw new Error("Remote runtime path is invalid");
  }
  const [pathname, ...queryParts] = path.split("?");
  if (queryParts.length > 1 || !pathname) {
    throw new Error("Remote runtime path is invalid");
  }
  const query: Record<string, string> = {};
  if (queryParts.length === 1) {
    const rawQuery = queryParts[0];
    if (!rawQuery) {
      throw new Error("Remote runtime query is invalid");
    }
    for (const pair of rawQuery.split("&")) {
      const separatorIndex = pair.indexOf("=");
      if (separatorIndex <= 0) {
        throw new Error("Remote runtime query is invalid");
      }
      let key: string;
      let value: string;
      try {
        key = decodeURIComponent(pair.slice(0, separatorIndex));
        value = decodeURIComponent(pair.slice(separatorIndex + 1));
      } catch {
        throw new Error("Remote runtime query is invalid");
      }
      if (key in query) {
        throw new Error("Remote runtime query keys must be unique");
      }
      query[key] = value;
    }
  }
  return { pathname, query };
}

function isCanonicalRunnerPublicKey(value: string): boolean {
  if (!/^[A-Za-z0-9_-]{43}$/u.test(value)) {
    return false;
  }
  try {
    const binary = atob(`${value.replace(/-/gu, "+").replace(/_/gu, "/")}=`);
    return (
      binary.length === 32 &&
      btoa(binary)
        .replace(/\+/gu, "-")
        .replace(/\//gu, "_")
        .replace(/=+$/u, "") === value
    );
  } catch {
    return false;
  }
}

function validateRuntime(runtime: RuntimeRef): void {
  if (runtime.location === "local" || runtime.location === "hosted") {
    return;
  }
  if (runtime.location === "managed") {
    if (!isCanonicalRunnerPublicKey(runtime.runnerPublicKey)) {
      throw new Error("Managed runner public key is invalid");
    }
    return;
  }
  if (!runtime.runnerId || runtime.runnerId.length > 256) {
    throw new Error("BYOC runner ID is invalid");
  }
  if (!runtime.runnerPublicKey) {
    throw new Error("BYOC runner public key is required");
  }
}

export function createRuntimeTransport(
  runtime: RuntimeRef,
  dependencies?: RuntimeTransportDependencies,
): RuntimeTransport {
  validateRuntime(runtime);
  const runtimeDependencies = dependencies ?? {
    apiClient:
      runtime.location === "hosted" && window.yinshiDesktop !== undefined
        ? hostedApi
        : api,
    encryptedRequest: requestEncryptedRunner,
    connectEncrypted: connectEncryptedRunner,
    now: Date.now,
  };
  if (
    !runtimeDependencies.apiClient ||
    typeof runtimeDependencies.apiClient.get !== "function"
  ) {
    throw new TypeError("Runtime API client is invalid");
  }
  if (typeof runtimeDependencies.encryptedRequest !== "function") {
    throw new TypeError("Encrypted runtime request must be callable");
  }
  if (
    runtimeDependencies.connectEncrypted !== undefined &&
    typeof runtimeDependencies.connectEncrypted !== "function"
  ) {
    throw new TypeError("Encrypted runtime connector must be callable");
  }
  interface ManagedConnectionEntry {
    connection: Promise<EncryptedRunnerConnection>;
    tail: Promise<void>;
    retired: boolean;
    successfulHistoryRequests: number;
  }

  const managedConnections = new Map<string, ManagedConnectionEntry>();
  let closed = false;

  async function managedRequest<T>(
    scope: string,
    method: RuntimeMethod,
    pathname: string,
    query: Readonly<Record<string, string>>,
    body: unknown,
  ): Promise<T> {
    if (closed) {
      throw new Error("Runtime transport is closed");
    }
    const connect =
      runtimeDependencies.connectEncrypted ?? connectEncryptedRunner;
    const connectionLane = managedConnectionLane(
      scope,
      method,
      pathname,
      query,
    );
    let entry = managedConnections.get(connectionLane);
    if (entry === undefined) {
      const connection = connect({
        expectedRunnerPublicKey:
          runtime.location === "managed" ? runtime.runnerPublicKey : "",
        scopes: [scope],
        maxSessionBytes: RUNTIME_TRANSPORT_SESSION_BYTES,
        capabilityEndpoint: "/api/runtime/capabilities",
      });
      entry = {
        connection,
        tail: Promise.resolve(),
        retired: false,
        successfulHistoryRequests: 0,
      };
      managedConnections.set(connectionLane, entry);
      void connection.catch(() => {
        if (managedConnections.get(connectionLane) === entry) {
          managedConnections.delete(connectionLane);
        }
      });
    }
    const activeEntry = entry;
    const operation = activeEntry.tail.then(async () => {
      let connection = await activeEntry.connection;
      if (closed) {
        connection.close();
        throw new Error("Runtime transport is closed");
      }
      const now = runtimeDependencies.now ?? Date.now;
      const connectionExpired =
        connection.expiresAtMs !== undefined && connection.expiresAtMs <= now();
      if (connectionExpired || activeEntry.retired) {
        connection.close();
        if (closed) {
          throw new Error("Runtime transport is closed");
        }
        const replacement = connect({
          expectedRunnerPublicKey:
            runtime.location === "managed" ? runtime.runnerPublicKey : "",
          scopes: [scope],
          maxSessionBytes: RUNTIME_TRANSPORT_SESSION_BYTES,
          capabilityEndpoint: "/api/runtime/capabilities",
        });
        activeEntry.connection = replacement;
        activeEntry.retired = false;
        activeEntry.successfulHistoryRequests = 0;
        try {
          connection = await replacement;
        } catch (error) {
          if (managedConnections.get(connectionLane) === activeEntry) {
            managedConnections.delete(connectionLane);
          }
          throw error;
        }
        if (closed) {
          connection.close();
          throw new Error("Runtime transport is closed");
        }
      }
      try {
        const result = await connection.request<T>({
          method,
          path: pathname,
          query,
          body,
        });
        if (isBoundedSessionHistoryRequest(method, pathname)) {
          activeEntry.successfulHistoryRequests += 1;
          if (
            activeEntry.successfulHistoryRequests >=
            MANAGED_HISTORY_REQUESTS_PER_CONNECTION
          ) {
            activeEntry.retired = true;
          }
        }
        return result;
      } catch (error) {
        if (managedConnections.get(connectionLane) === activeEntry) {
          managedConnections.delete(connectionLane);
        }
        connection.close();
        throw error;
      }
    });
    activeEntry.tail = operation.then(
      () => undefined,
      () => undefined,
    );
    return operation;
  }

  async function request<T>(
    method: RuntimeMethod,
    path: string,
    body?: unknown,
  ): Promise<T> {
    if (runtime.location === "byoc" || runtime.location === "managed") {
      const parsedPath = parseRemotePath(path);
      const scope = requiredScope(method, parsedPath.pathname);
      if (runtime.location === "managed") {
        return managedRequest<T>(
          scope,
          method,
          parsedPath.pathname,
          parsedPath.query,
          body ?? null,
        );
      }
      return runtimeDependencies.encryptedRequest<T>({
        expectedRunnerPublicKey: runtime.runnerPublicKey,
        scopes: [scope],
        method,
        path: parsedPath.pathname,
        query: parsedPath.query,
        body: body ?? null,
        maxSessionBytes: RUNTIME_TRANSPORT_SESSION_BYTES,
      });
    }
    if (method === "GET") {
      return runtimeDependencies.apiClient.get<T>(path);
    }
    if (method === "POST") {
      return runtimeDependencies.apiClient.post<T>(path, body);
    }
    if (method === "PATCH") {
      return runtimeDependencies.apiClient.patch<T>(path, body);
    }
    if (method === "PUT") {
      return runtimeDependencies.apiClient.put<T>(path, body);
    }
    await runtimeDependencies.apiClient.delete(path);
    return undefined as T;
  }

  return {
    runtime,
    get: <T>(path: string) => request<T>("GET", path),
    post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
    patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
    put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
    delete: (path: string) => request<void>("DELETE", path),
    upload: <T>(path: string, file: File) => {
      if (runtime.location === "byoc" || runtime.location === "managed") {
        if (path !== "/api/settings/pi-config/upload") {
          return Promise.reject(
            new Error("Remote runtime upload path is not allowed"),
          );
        }
        return uploadEncryptedPiConfig<T>(runtime, file);
      }
      if (runtime.location === "hosted" && window.yinshiDesktop !== undefined) {
        if (path !== "/api/settings/pi-config/upload") {
          return Promise.reject(
            new Error("Hosted runtime upload path is not allowed"),
          );
        }
        return uploadHostedPiConfig<T>(file);
      }
      return runtimeDependencies.apiClient.upload<T>(path, file);
    },
    close: () => {
      if (closed) return;
      closed = true;
      for (const entry of managedConnections.values()) {
        void entry.connection.then(
          (connection) => connection.close(),
          () => undefined,
        );
      }
      managedConnections.clear();
    },
  };
}
