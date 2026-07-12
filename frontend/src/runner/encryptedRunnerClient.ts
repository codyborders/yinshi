import { api } from "../api/client";
import {
  createNoiseIkInitiator,
  createNoiseIkKeypair,
  type NoiseIkInitiator,
  type NoiseIkKeypair,
} from "../crypto/noiseIk";

const RUNNER_PROTOCOL = "yinshi-runner-v1";
const RUNNER_HEALTH_SESSION_BYTES = 65_536;
const SOCKET_TIMEOUT_MS = 15_000;
const RUNNER_PUBLIC_KEY_BYTES = 32;

export interface RunnerCapabilityResponse {
  readonly capability: string;
  readonly transfer_id: string;
  readonly runner_id: string;
  readonly runner_public_key: string;
  readonly protocol: string;
  readonly issued_at: number;
  readonly expires_at: number;
  readonly max_frame_bytes: number;
  readonly max_session_bytes: number;
  readonly relay_url: string;
}

export interface RunnerCapabilityRequest {
  readonly initiator_public_key: string;
  readonly scopes: readonly string[];
  readonly max_session_bytes: number;
}

export interface RunnerHealth {
  readonly protocol: string;
  readonly status: "ok";
}

export interface EncryptedRunnerConnectionOptions {
  readonly expectedRunnerPublicKey: string;
  readonly scopes: readonly string[];
  readonly maxSessionBytes?: number;
}

export interface EncryptedRunnerOperation {
  readonly method: "DELETE" | "GET" | "PATCH" | "POST" | "PUT";
  readonly path: string;
  readonly query?: Readonly<Record<string, string>>;
  readonly body?: unknown;
}

export interface EncryptedRunnerRequest
  extends EncryptedRunnerConnectionOptions, EncryptedRunnerOperation {}

export interface EncryptedRunnerConnection {
  request<T>(operation: EncryptedRunnerOperation): Promise<T>;
  close(): void;
}

export class RunnerRpcError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown) {
    super(`Runner RPC failed with status ${status}`);
    this.name = "RunnerRpcError";
    this.status = status;
    this.body = body;
  }
}

export interface RunnerClientDependencies {
  createKeypair(): Promise<NoiseIkKeypair>;
  createInitiator(options: {
    readonly staticPrivateKey: Uint8Array;
    readonly responderStaticPublicKey: Uint8Array;
    readonly prologue: Uint8Array;
  }): Promise<NoiseIkInitiator>;
  issueCapability(
    request: RunnerCapabilityRequest,
  ): Promise<RunnerCapabilityResponse>;
  openWebSocket(url: string): WebSocket;
  createRequestId(): string;
}

function encodeBase64url(value: Uint8Array): string {
  if (!(value instanceof Uint8Array) || value.length === 0) {
    throw new TypeError("base64url input must be a non-empty Uint8Array");
  }
  let binary = "";
  for (const byte of value) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function decodePublicKey(value: string): Uint8Array {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(value)) {
    throw new Error("Runner public key is not canonical base64url");
  }
  let binary: string;
  try {
    binary = atob(`${value.replace(/-/g, "+").replace(/_/g, "/")}=`);
  } catch {
    throw new Error("Runner public key is not canonical base64url");
  }
  const key = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  if (
    key.length !== RUNNER_PUBLIC_KEY_BYTES ||
    encodeBase64url(key) !== value
  ) {
    throw new Error("Runner public key must contain 32 canonical bytes");
  }
  return key;
}

function validateCapability(
  value: RunnerCapabilityResponse,
  expectedRunnerPublicKey: string,
  expectedSessionBytes: number,
): RunnerCapabilityResponse {
  if (value === null || typeof value !== "object") {
    throw new Error("Runner capability response is invalid");
  }
  if (value.protocol !== RUNNER_PROTOCOL) {
    throw new Error("Runner capability protocol is unsupported");
  }
  if (value.runner_public_key !== expectedRunnerPublicKey) {
    throw new Error("Runner identity changed; pairing is required again");
  }
  decodePublicKey(value.runner_public_key);
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      value.transfer_id,
    )
  ) {
    throw new Error("Runner capability transfer ID is invalid");
  }
  if (
    !Number.isSafeInteger(value.issued_at) ||
    !Number.isSafeInteger(value.expires_at) ||
    value.expires_at <= Math.floor(Date.now() / 1_000)
  ) {
    throw new Error("Runner capability is expired or invalid");
  }
  if (
    value.max_frame_bytes !== 65_535 ||
    value.max_session_bytes !== expectedSessionBytes
  ) {
    throw new Error("Runner capability limits are invalid");
  }
  const relayUrl = new URL(value.relay_url);
  if (
    !["ws:", "wss:"].includes(relayUrl.protocol) ||
    relayUrl.username !== "" ||
    relayUrl.password !== "" ||
    relayUrl.search !== "" ||
    relayUrl.hash !== "" ||
    relayUrl.pathname !== `/api/runner/relay/${value.transfer_id}`
  ) {
    throw new Error("Runner relay URL is invalid");
  }
  if (
    relayUrl.protocol !== "wss:" &&
    window.location.hostname !== "localhost"
  ) {
    throw new Error("Runner relay URL must use TLS");
  }
  if (typeof value.capability !== "string" || value.capability.length < 64) {
    throw new Error("Runner capability token is invalid");
  }
  return value;
}

function waitForOpen(socket: WebSocket): Promise<void> {
  if (socket.readyState === WebSocket.OPEN) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      window.clearTimeout(timeout);
      socket.removeEventListener("open", handleOpen);
      socket.removeEventListener("error", handleError);
      socket.removeEventListener("close", handleClose);
    };
    const handleOpen = () => {
      cleanup();
      resolve();
    };
    const handleError = () => {
      cleanup();
      reject(new Error("Runner relay could not be opened"));
    };
    const handleClose = () => {
      cleanup();
      reject(new Error("Runner relay closed before opening"));
    };
    const timeout = window.setTimeout(() => {
      cleanup();
      reject(new Error("Runner relay open timed out"));
    }, SOCKET_TIMEOUT_MS);
    socket.addEventListener("open", handleOpen);
    socket.addEventListener("error", handleError);
    socket.addEventListener("close", handleClose);
  });
}

function receiveMessage(socket: WebSocket): Promise<string | ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      window.clearTimeout(timeout);
      socket.removeEventListener("message", handleMessage);
      socket.removeEventListener("close", handleClose);
      socket.removeEventListener("error", handleError);
    };
    const handleMessage = (event: MessageEvent) => {
      cleanup();
      if (typeof event.data === "string" || event.data instanceof ArrayBuffer) {
        resolve(event.data);
        return;
      }
      reject(new Error("Runner relay returned an unsupported frame"));
    };
    const handleClose = () => {
      cleanup();
      reject(new Error("Runner relay closed before responding"));
    };
    const handleError = () => {
      cleanup();
      reject(new Error("Runner relay failed before responding"));
    };
    const timeout = window.setTimeout(() => {
      cleanup();
      reject(new Error("Runner relay response timed out"));
    }, SOCKET_TIMEOUT_MS);
    socket.addEventListener("message", handleMessage);
    socket.addEventListener("close", handleClose);
    socket.addEventListener("error", handleError);
  });
}

async function sendAndReceive(
  socket: WebSocket,
  message: string | Uint8Array,
): Promise<string | ArrayBuffer> {
  const response = receiveMessage(socket);
  socket.send(message);
  return response;
}

function parseHandshakeResponse(
  value: Uint8Array,
  expectedTransferId: string,
): void {
  let payload: unknown;
  try {
    payload = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(value),
    );
  } catch {
    throw new Error("Runner handshake response is invalid");
  }
  if (
    payload === null ||
    typeof payload !== "object" ||
    Object.keys(payload).length !== 2 ||
    !("protocol" in payload) ||
    payload.protocol !== RUNNER_PROTOCOL ||
    !("transfer_id" in payload) ||
    payload.transfer_id !== expectedTransferId
  ) {
    throw new Error("Runner handshake binding did not match the capability");
  }
}

function parseRpcResponse(
  value: Uint8Array,
  requestId: string,
  expectedSequence: number,
): { status: number; body: unknown } {
  let payload: unknown;
  try {
    payload = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(value),
    );
  } catch {
    throw new Error("Runner RPC response is invalid");
  }
  if (payload === null || typeof payload !== "object") {
    throw new Error("Runner RPC response is invalid");
  }
  const response = payload as Record<string, unknown>;
  if (
    Object.keys(response).length !== 6 ||
    response.v !== 2 ||
    response.type !== "response" ||
    response.sequence !== expectedSequence ||
    response.request_id !== requestId ||
    !Number.isSafeInteger(response.status) ||
    (response.status as number) < 100 ||
    (response.status as number) > 599
  ) {
    throw new Error("Runner RPC response did not match the request");
  }
  return { status: response.status as number, body: response.body };
}

function validateHealth(value: unknown): RunnerHealth {
  if (value === null || typeof value !== "object") {
    throw new Error("Runner health response body is invalid");
  }
  const health = value as Record<string, unknown>;
  if (
    Object.keys(health).length !== 2 ||
    health.protocol !== RUNNER_PROTOCOL ||
    health.status !== "ok"
  ) {
    throw new Error("Runner health response body is invalid");
  }
  return { protocol: RUNNER_PROTOCOL, status: "ok" };
}

const defaultDependencies: RunnerClientDependencies = {
  createKeypair: createNoiseIkKeypair,
  createInitiator: createNoiseIkInitiator,
  issueCapability: (request) =>
    api.post<RunnerCapabilityResponse>(
      "/api/settings/runner/capabilities",
      request,
    ),
  openWebSocket: (url) => new WebSocket(url),
  createRequestId: () => crypto.randomUUID(),
};

function validateOperation(operation: EncryptedRunnerOperation): {
  readonly method: EncryptedRunnerOperation["method"];
  readonly path: string;
  readonly query: Readonly<Record<string, string>>;
  readonly body: unknown;
} {
  const { method, path, query = {}, body = null } = operation;
  if (!path.startsWith("/") || path.includes("?") || path.includes("#")) {
    throw new TypeError("Runner request path must be normalized");
  }
  const queryEntries = Object.entries(query);
  if (queryEntries.length > 16)
    throw new TypeError("Runner request query is too large");
  for (const [key, value] of queryEntries) {
    if (
      !/^[A-Za-z0-9_]{1,64}$/u.test(key) ||
      typeof value !== "string" ||
      value.length > 2_048
    ) {
      throw new TypeError("Runner request query is invalid");
    }
  }
  if (!new Set(["DELETE", "GET", "PATCH", "POST", "PUT"]).has(method)) {
    throw new TypeError("Runner request method is invalid");
  }
  return { method, path, query, body };
}

export async function connectEncryptedRunner(
  options: EncryptedRunnerConnectionOptions,
  dependencies: RunnerClientDependencies = defaultDependencies,
): Promise<EncryptedRunnerConnection> {
  const {
    expectedRunnerPublicKey,
    scopes,
    maxSessionBytes = RUNNER_HEALTH_SESSION_BYTES,
  } = options;
  decodePublicKey(expectedRunnerPublicKey);
  if (
    !Array.isArray(scopes) ||
    scopes.length === 0 ||
    scopes.some((scope) => !scope)
  ) {
    throw new TypeError("Runner request scopes must not be empty");
  }
  if (
    !Number.isSafeInteger(maxSessionBytes) ||
    maxSessionBytes < 65_536 ||
    maxSessionBytes > 1_073_741_824
  ) {
    throw new RangeError("Runner session byte limit is invalid");
  }

  const keypair = await dependencies.createKeypair();
  if (keypair.privateKey.length !== 32 || keypair.publicKey.length !== 32) {
    keypair.privateKey.fill(0);
    throw new Error("Runner client keypair is invalid");
  }
  const clientPublicKey = encodeBase64url(keypair.publicKey);
  let capability: RunnerCapabilityResponse;
  let initiator: NoiseIkInitiator;
  try {
    capability = validateCapability(
      await dependencies.issueCapability({
        initiator_public_key: clientPublicKey,
        scopes,
        max_session_bytes: maxSessionBytes,
      }),
      expectedRunnerPublicKey,
      maxSessionBytes,
    );
    initiator = await dependencies.createInitiator({
      staticPrivateKey: keypair.privateKey,
      responderStaticPublicKey: decodePublicKey(capability.runner_public_key),
      prologue: new TextEncoder().encode(RUNNER_PROTOCOL),
    });
  } finally {
    keypair.privateKey.fill(0);
  }

  let socket: WebSocket;
  try {
    socket = dependencies.openWebSocket(capability.relay_url);
  } catch (error) {
    initiator.dispose();
    throw error;
  }
  socket.binaryType = "arraybuffer";
  let closed = false;
  let requestPending = false;
  let nextSequence = 0;
  const close = () => {
    if (closed) return;
    closed = true;
    initiator.dispose();
    if (
      socket.readyState === WebSocket.OPEN ||
      socket.readyState === WebSocket.CONNECTING
    ) {
      socket.close(1000, "Runner connection complete");
    }
  };

  try {
    await waitForOpen(socket);
    const ready = await sendAndReceive(socket, capability.capability);
    if (ready !== '{"type":"ready"}')
      throw new Error("Runner relay did not become ready");
    const handshakeMessage = initiator.writeHandshakeMessage(
      new TextEncoder().encode(capability.capability),
    );
    const handshakeResponse = await sendAndReceive(socket, handshakeMessage);
    if (!(handshakeResponse instanceof ArrayBuffer)) {
      throw new Error("Runner handshake response must be binary");
    }
    parseHandshakeResponse(
      initiator.readHandshakeMessage(new Uint8Array(handshakeResponse)),
      capability.transfer_id,
    );
  } catch (error) {
    close();
    throw error;
  }

  return {
    async request<T>(operation: EncryptedRunnerOperation): Promise<T> {
      if (closed) throw new Error("Runner connection is closed");
      if (requestPending)
        throw new Error("Runner connection requests must be sequential");
      if (nextSequence >= 1_048_576) {
        close();
        throw new Error("Runner connection reached its message limit");
      }
      const { method, path, query, body } = validateOperation(operation);
      requestPending = true;
      const sequence = nextSequence;
      try {
        const requestId = dependencies.createRequestId();
        const request = new TextEncoder().encode(
          JSON.stringify({
            body,
            method,
            path,
            query,
            request_id: requestId,
            sequence,
            type: "request",
            v: 2,
          }),
        );
        if (request.length > 49_152)
          throw new Error("Runner RPC request is too large");
        const encryptedResponse = await sendAndReceive(
          socket,
          initiator.encrypt(request),
        );
        if (!(encryptedResponse instanceof ArrayBuffer)) {
          throw new Error("Runner RPC response must be binary");
        }
        const response = parseRpcResponse(
          initiator.decrypt(new Uint8Array(encryptedResponse)),
          requestId,
          sequence,
        );
        nextSequence += 1;
        if (response.status < 200 || response.status > 299) {
          throw new RunnerRpcError(response.status, response.body);
        }
        return response.body as T;
      } catch (error) {
        if (!(error instanceof RunnerRpcError)) close();
        throw error;
      } finally {
        requestPending = false;
      }
    },
    close,
  };
}

export async function requestEncryptedRunner<T>(
  requestOptions: EncryptedRunnerRequest,
  dependencies: RunnerClientDependencies = defaultDependencies,
): Promise<T> {
  const {
    expectedRunnerPublicKey,
    scopes,
    maxSessionBytes,
    method,
    path,
    query,
    body,
  } = requestOptions;
  const connection = await connectEncryptedRunner(
    { expectedRunnerPublicKey, scopes, maxSessionBytes },
    dependencies,
  );
  try {
    return await connection.request<T>({ method, path, query, body });
  } finally {
    connection.close();
  }
}

export async function checkEncryptedRunnerHealth(
  expectedRunnerPublicKey: string,
  dependencies: RunnerClientDependencies = defaultDependencies,
): Promise<RunnerHealth> {
  const response = await requestEncryptedRunner<unknown>(
    {
      expectedRunnerPublicKey,
      scopes: ["worker.health"],
      method: "GET",
      path: "/health",
    },
    dependencies,
  );
  return validateHealth(response);
}
