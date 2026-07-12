import { api, hostedApi } from "../api/client";
import {
  uploadEncryptedPiConfig,
  uploadHostedPiConfig,
} from "./encryptedUpload";
import {
  requestEncryptedRunner,
  type EncryptedRunnerRequest,
} from "../runner/encryptedRunnerClient";

export type RuntimeRef =
  | { readonly location: "local" }
  | { readonly location: "hosted" }
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

interface RuntimeTransportDependencies {
  readonly apiClient: JsonApiClient;
  readonly encryptedRequest: EncryptedRequest;
}

export interface RuntimeTransport {
  readonly runtime: RuntimeRef;
  get<T>(path: string): Promise<T>;
  post<T>(path: string, body?: unknown): Promise<T>;
  patch<T>(path: string, body?: unknown): Promise<T>;
  put<T>(path: string, body?: unknown): Promise<T>;
  delete(path: string): Promise<void>;
  upload<T>(path: string, file: File): Promise<T>;
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
const PROMPT_RUN_COLLECTION_PATH = new RegExp(
  `^/api/sessions/${RESOURCE_ID}/runs$`,
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
const BYOC_SESSION_BYTES = 262_144;

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
  if (method === "GET" && (sessionCollection || sessionMember || sessionRead)) {
    return "session.read";
  }
  if (method === "POST" && sessionCollection) {
    return "session.write";
  }
  if (method === "PATCH" && sessionMember) {
    return "session.write";
  }

  const promptRunCollection = PROMPT_RUN_COLLECTION_PATH.test(path);
  const promptRunEvents = PROMPT_RUN_EVENTS_PATH.test(path);
  const promptRunCancel = PROMPT_RUN_CANCEL_PATH.test(path);
  if (method === "POST" && (promptRunCollection || promptRunCancel)) {
    return "session.stream";
  }
  if (method === "GET" && promptRunEvents) {
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
  throw new Error("BYOC runtime method or path is not allowed");
}

function parseByocPath(path: string): {
  readonly pathname: string;
  readonly query: Readonly<Record<string, string>>;
} {
  if (typeof path !== "string" || !path.startsWith("/") || path.includes("#")) {
    throw new Error("BYOC runtime path is invalid");
  }
  const [pathname, ...queryParts] = path.split("?");
  if (queryParts.length > 1 || !pathname) {
    throw new Error("BYOC runtime path is invalid");
  }
  const query: Record<string, string> = {};
  if (queryParts.length === 1) {
    const rawQuery = queryParts[0];
    if (!rawQuery) {
      throw new Error("BYOC runtime query is invalid");
    }
    for (const pair of rawQuery.split("&")) {
      const separatorIndex = pair.indexOf("=");
      if (separatorIndex <= 0) {
        throw new Error("BYOC runtime query is invalid");
      }
      let key: string;
      let value: string;
      try {
        key = decodeURIComponent(pair.slice(0, separatorIndex));
        value = decodeURIComponent(pair.slice(separatorIndex + 1));
      } catch {
        throw new Error("BYOC runtime query is invalid");
      }
      if (key in query) {
        throw new Error("BYOC runtime query keys must be unique");
      }
      query[key] = value;
    }
  }
  return { pathname, query };
}

function validateRuntime(runtime: RuntimeRef): void {
  if (runtime.location === "local" || runtime.location === "hosted") {
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

  async function request<T>(
    method: RuntimeMethod,
    path: string,
    body?: unknown,
  ): Promise<T> {
    if (runtime.location === "byoc") {
      const parsedPath = parseByocPath(path);
      const scope = requiredScope(method, parsedPath.pathname);
      return runtimeDependencies.encryptedRequest<T>({
        expectedRunnerPublicKey: runtime.runnerPublicKey,
        scopes: [scope],
        method,
        path: parsedPath.pathname,
        query: parsedPath.query,
        body: body ?? null,
        maxSessionBytes: BYOC_SESSION_BYTES,
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
      if (runtime.location === "byoc") {
        if (path !== "/api/settings/pi-config/upload") {
          return Promise.reject(
            new Error("BYOC runtime upload path is not allowed"),
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
  };
}
