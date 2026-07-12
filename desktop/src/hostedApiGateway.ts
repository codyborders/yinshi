import type { HostedApiRequest, HostedApiResponse } from "./desktopApi.js";

const RESPONSE_BYTES_MAX = 1_048_576;
const REQUEST_BYTES_MAX = 65_536;
const REQUEST_TIMEOUT_MS = 15_000;

const ALLOWED_HOSTED_REQUESTS = new Set([
  "DELETE /api/settings/runner",
  "GET /api/settings/runner",
  "POST /api/settings/runner",
  "POST /api/settings/runner/capabilities",
  "POST /api/settings/runner/noise-key/confirm",
]);

export interface HostedApiGatewayOptions {
  readonly apiBaseUrl: string;
  readonly fetch: (input: string | URL, init?: RequestInit) => Promise<Response>;
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
    throw new TypeError("apiBaseUrl must be an HTTPS URL without credentials, query, or fragment");
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
  if (!ALLOWED_HOSTED_REQUESTS.has(`${value.method} ${value.path}`)) {
    throw new Error("Hosted API route is not allowed");
  }
  if ((value.method === "GET" || value.method === "DELETE") && value.body !== undefined) {
    throw new TypeError(`${value.method} hosted requests cannot include a body`);
  }
  if (value.method !== "POST") {
    return { method: value.method, path: value.path, bodyText: undefined };
  }
  if (
    value.body === undefined ||
    value.body === null ||
    typeof value.body !== "object" ||
    Array.isArray(value.body)
  ) {
    throw new TypeError("POST hosted requests require a JSON object body");
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
    if (typeof options.fetch !== "function" || typeof options.getAccessToken !== "function") {
      throw new TypeError("HostedApiGateway requires fetch and getAccessToken functions");
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
    const contentType = response.headers.get("content-type")?.split(";", 1)[0]?.trim();
    if (contentType !== "application/json") {
      throw new Error("Hosted API response must be JSON");
    }
    let body: unknown;
    try {
      body = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bodyBytes));
    } catch {
      throw new Error("Hosted API response contained invalid JSON");
    }
    return { status: response.status, body };
  }
}
