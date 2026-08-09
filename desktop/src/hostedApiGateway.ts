import type { HostedApiRequest, HostedApiResponse } from "./desktopApi.js";

const RESPONSE_BYTES_MAX = 1_048_576;
const REQUEST_BYTES_MAX = 65_536;
const REQUEST_TIMEOUT_MS = 15_000;

const RESOURCE_ID = "[0-9a-f]{32}";
const RUNNER_REQUESTS = new Set([
  "DELETE /api/settings/runner",
  "GET /api/settings/runner",
  "POST /api/settings/runner",
  "POST /api/settings/runner/capabilities",
  "POST /api/settings/runner/noise-key/confirm",
]);

function routeAllowed(
  method: HostedApiRequest["method"],
  pathValue: string,
): boolean {
  if (
    !pathValue.startsWith("/") ||
    pathValue.startsWith("//") ||
    pathValue.includes("\\")
  ) {
    return false;
  }
  let routeUrl: URL;
  try {
    routeUrl = new URL(pathValue, "https://hosted.invalid");
  } catch {
    return false;
  }
  if (routeUrl.origin !== "https://hosted.invalid" || routeUrl.hash)
    return false;
  const path = routeUrl.pathname;
  if (pathValue.split("?", 1)[0] !== path) return false;
  const queryKeys = Array.from(routeUrl.searchParams.keys());
  const isFileRoute = new RegExp(
    `^/api/workspaces/${RESOURCE_ID}/files/(?:content|diff|preview)$`,
  ).test(path);
  const isProviderCallback = new RegExp(
    "^/auth/providers/[a-z0-9-]{1,64}/callback$",
  ).test(path);
  if (queryKeys.length > 0) {
    const fileQuery =
      isFileRoute &&
      queryKeys.length === 1 &&
      queryKeys[0] === "path" &&
      routeUrl.searchParams.getAll("path").length === 1 &&
      Boolean(routeUrl.searchParams.get("path")) &&
      (routeUrl.searchParams.get("path")?.length ?? 0) <= 2_048;
    const providerQuery =
      isProviderCallback &&
      queryKeys.length === 1 &&
      queryKeys[0] === "flow_id" &&
      routeUrl.searchParams.getAll("flow_id").length === 1 &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
        routeUrl.searchParams.get("flow_id") ?? "",
      );
    if (!fileQuery && !providerQuery) return false;
  }
  if (RUNNER_REQUESTS.has(`${method} ${path}`)) return queryKeys.length === 0;
  if (
    method === "GET" &&
    ["/api/catalog", "/api/github/installations"].includes(path)
  ) {
    return queryKeys.length === 0;
  }
  if (method === "GET" && path === "/api/repos") return true;
  if (method === "POST" && path === "/api/repos") return true;
  if (new RegExp(`^/api/repos/${RESOURCE_ID}$`).test(path)) {
    return ["DELETE", "GET", "PATCH"].includes(method);
  }
  if (new RegExp(`^/api/repos/${RESOURCE_ID}/workspaces$`).test(path)) {
    return method === "GET" || method === "POST";
  }
  if (new RegExp(`^/api/workspaces/${RESOURCE_ID}$`).test(path)) {
    return method === "DELETE" || method === "PATCH";
  }
  if (new RegExp(`^/api/workspaces/${RESOURCE_ID}/sessions$`).test(path)) {
    return method === "GET" || method === "POST";
  }
  if (new RegExp(`^/api/sessions/${RESOURCE_ID}$`).test(path)) {
    return method === "GET" || method === "PATCH";
  }
  if (
    new RegExp(`^/api/sessions/${RESOURCE_ID}/(?:messages|tree)$`).test(path)
  ) {
    return method === "GET";
  }
  if (new RegExp(`^/api/sessions/${RESOURCE_ID}/runs$`).test(path))
    return method === "POST";
  if (
    new RegExp(
      `^/api/sessions/${RESOURCE_ID}/runs/${RESOURCE_ID}/events/[0-9]{1,6}$`,
    ).test(path)
  ) {
    return method === "GET";
  }
  if (
    new RegExp(
      `^/api/sessions/${RESOURCE_ID}/runs/${RESOURCE_ID}/cancel$`,
    ).test(path)
  ) {
    return method === "POST";
  }
  if (
    new RegExp(
      `^/api/workspaces/${RESOURCE_ID}/files/(?:changed|diff|preview|tree)$`,
    ).test(path)
  ) {
    return method === "GET";
  }
  if (new RegExp(`^/api/workspaces/${RESOURCE_ID}/files/content$`).test(path)) {
    return method === "PUT";
  }
  if (new RegExp(`^/api/workspaces/${RESOURCE_ID}/terminals$`).test(path)) {
    return method === "POST";
  }
  if (
    new RegExp(
      `^/api/workspaces/${RESOURCE_ID}/terminals/${RESOURCE_ID}$`,
    ).test(path)
  ) {
    return method === "DELETE";
  }
  if (
    new RegExp(
      `^/api/workspaces/${RESOURCE_ID}/terminals/${RESOURCE_ID}/(?:input|resize|restart)$`,
    ).test(path)
  ) {
    return method === "POST";
  }
  if (
    new RegExp(
      `^/api/workspaces/${RESOURCE_ID}/terminals/${RESOURCE_ID}/events/[0-9]{1,10}$`,
    ).test(path)
  ) {
    return method === "GET";
  }
  if (path === "/api/settings/connections")
    return method === "GET" || method === "POST";
  if (new RegExp(`^/api/settings/connections/${RESOURCE_ID}$`).test(path)) {
    return method === "DELETE";
  }
  if (path === "/api/settings/pi-config")
    return method === "GET" || method === "DELETE";
  if (
    [
      "/api/settings/pi-config/commands",
      "/api/settings/pi-release-notes",
    ].includes(path)
  ) {
    return method === "GET";
  }
  if (
    ["/api/settings/pi-config/github", "/api/settings/pi-config/sync"].includes(
      path,
    )
  ) {
    return method === "POST";
  }
  if (path === "/api/settings/pi-config/categories") return method === "PATCH";
  if (path === "/api/settings/pi-config/uploads") return method === "POST";
  if (
    new RegExp(
      `^/api/settings/pi-config/uploads/${RESOURCE_ID}/chunks/[0-9]{1,5}$`,
    ).test(path)
  ) {
    return method === "POST";
  }
  if (
    new RegExp(
      `^/api/settings/pi-config/uploads/${RESOURCE_ID}/complete$`,
    ).test(path)
  ) {
    return method === "POST";
  }
  if (
    new RegExp(`^/api/settings/pi-config/uploads/${RESOURCE_ID}$`).test(path)
  ) {
    return method === "DELETE";
  }
  if (new RegExp("^/auth/providers/[a-z0-9-]{1,64}/start$").test(path)) {
    return method === "POST";
  }
  if (isProviderCallback) {
    if (method === "POST") return queryKeys.length === 0;
    if (method !== "GET") return false;
    return (
      queryKeys.length === 1 &&
      queryKeys[0] === "flow_id" &&
      routeUrl.searchParams.getAll("flow_id").length === 1 &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
        routeUrl.searchParams.get("flow_id") ?? "",
      )
    );
  }
  return false;
}

export interface HostedApiGatewayOptions {
  readonly apiBaseUrl: string;
  readonly fetch: (
    input: string | URL,
    init?: RequestInit,
  ) => Promise<Response>;
  readonly getAccessToken: () => Promise<string>;
}

function validateBaseUrl(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new TypeError("apiBaseUrl must be a valid URL");
  }
  if (
    url.protocol !== "https:" ||
    url.username !== "" ||
    url.password !== "" ||
    url.search !== "" ||
    url.hash !== ""
  ) {
    throw new TypeError(
      "apiBaseUrl must be an HTTPS URL without credentials, query, or fragment",
    );
  }
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/`;
  return url;
}

function validateRequest(value: HostedApiRequest): {
  method: HostedApiRequest["method"];
  path: string;
  bodyText: string | undefined;
} {
  if (value === null || typeof value !== "object") {
    throw new TypeError("Hosted API request must be an object");
  }
  const keys = Object.keys(value);
  if (keys.some((key) => !["body", "method", "path"].includes(key))) {
    throw new TypeError("Hosted API request contains unsupported fields");
  }
  if (!routeAllowed(value.method, value.path)) {
    throw new Error("Hosted API route is not allowed");
  }
  if (
    (value.method === "GET" || value.method === "DELETE") &&
    value.body !== undefined
  ) {
    throw new TypeError(
      `${value.method} hosted requests cannot include a body`,
    );
  }
  if (value.body === undefined) {
    if (value.method === "PATCH" || value.method === "PUT") {
      throw new TypeError(
        `${value.method} hosted requests require a JSON object body`,
      );
    }
    return { method: value.method, path: value.path, bodyText: undefined };
  }
  if (
    value.body === null ||
    typeof value.body !== "object" ||
    Array.isArray(value.body)
  ) {
    throw new TypeError("Hosted API request body must be a JSON object");
  }
  const bodyText = JSON.stringify(value.body);
  if (new TextEncoder().encode(bodyText).length > REQUEST_BYTES_MAX) {
    throw new RangeError("Hosted API request exceeded the size limit");
  }
  return { method: value.method, path: value.path, bodyText };
}

async function readBoundedBody(response: Response): Promise<Uint8Array> {
  const declaredLength = response.headers.get("content-length");
  if (declaredLength !== null) {
    const parsedLength = Number.parseInt(declaredLength, 10);
    if (!Number.isSafeInteger(parsedLength) || parsedLength < 0) {
      throw new Error("Hosted API response Content-Length is invalid");
    }
    if (parsedLength > RESPONSE_BYTES_MAX) {
      throw new Error("Hosted API response exceeded the size limit");
    }
  }
  if (response.body === null) {
    return new Uint8Array();
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let bytesRead = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      bytesRead += value.length;
      if (bytesRead > RESPONSE_BYTES_MAX) {
        await reader.cancel();
        throw new Error("Hosted API response exceeded the size limit");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(bytesRead);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.length;
  }
  return body;
}

export class HostedApiGateway {
  readonly #apiBaseUrl: URL;
  readonly #fetch: HostedApiGatewayOptions["fetch"];
  readonly #getAccessToken: HostedApiGatewayOptions["getAccessToken"];

  constructor(options: HostedApiGatewayOptions) {
    this.#apiBaseUrl = validateBaseUrl(options.apiBaseUrl);
    if (
      typeof options.fetch !== "function" ||
      typeof options.getAccessToken !== "function"
    ) {
      throw new TypeError(
        "HostedApiGateway requires fetch and getAccessToken functions",
      );
    }
    this.#fetch = options.fetch;
    this.#getAccessToken = options.getAccessToken;
  }

  async request(requestValue: HostedApiRequest): Promise<HostedApiResponse> {
    const request = validateRequest(requestValue);
    const accessToken = await this.#getAccessToken();
    if (typeof accessToken !== "string" || accessToken.length < 32) {
      throw new Error("Hosted API access token is unavailable");
    }
    const headers: Record<string, string> = {
      Accept: "application/json",
      Authorization: `Bearer ${accessToken}`,
    };
    if (request.method !== "GET") {
      headers["X-Requested-With"] = "XMLHttpRequest";
    }
    if (request.bodyText !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    const requestInit: RequestInit = {
      method: request.method,
      headers,
      redirect: "error",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    };
    if (request.bodyText !== undefined) {
      requestInit.body = request.bodyText;
    }
    const response = await this.#fetch(
      new URL(request.path.slice(1), this.#apiBaseUrl),
      requestInit,
    );
    const bodyBytes = await readBoundedBody(response);
    if (response.status === 204) {
      if (bodyBytes.length !== 0) {
        throw new Error("Hosted API 204 response included a body");
      }
      return { status: response.status, body: null };
    }
    const contentType = response.headers
      .get("content-type")
      ?.split(";", 1)[0]
      ?.trim();
    if (contentType !== "application/json") {
      throw new Error("Hosted API response must be JSON");
    }
    let body: unknown;
    try {
      body = JSON.parse(
        new TextDecoder("utf-8", { fatal: true }).decode(bodyBytes),
      );
    } catch {
      throw new Error("Hosted API response contained invalid JSON");
    }
    return { status: response.status, body };
  }
}
