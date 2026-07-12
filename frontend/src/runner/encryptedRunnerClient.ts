import { api } from "../api/client";
import {
  createNoiseIkInitiator,
  createNoiseIkKeypair,
  type NoiseIkInitiator,
  type NoiseIkKeypair,
} from "../crypto/noiseIk";

const RUNNER_PROTOCOL = "yinshi-runner-v1";
const RUNNER_SESSION_BYTES = 65_536;
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

export interface RunnerClientDependencies {
  createKeypair(): Promise<NoiseIkKeypair>;
  createInitiator(options: {
    readonly staticPrivateKey: Uint8Array;
    readonly responderStaticPublicKey: Uint8Array;
    readonly prologue: Uint8Array;
  }): Promise<NoiseIkInitiator>;
  issueCapability(request: RunnerCapabilityRequest): Promise<RunnerCapabilityResponse>;
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
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decodePublicKey(value: string): Uint8Array {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(value)) {
    throw new Error("Runner public key is not canonical base64url");
  }
  let binary: string;
  try {
    binary = atob(`${value.replace(/-/g, "+").replace(/_/g, "/") }=`);
  } catch {
    throw new Error("Runner public key is not canonical base64url");
  }
  const key = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  if (key.length !== RUNNER_PUBLIC_KEY_BYTES || encodeBase64url(key) !== value) {
    throw new Error("Runner public key must contain 32 canonical bytes");
  }
  return key;
}

function validateCapability(
  value: RunnerCapabilityResponse,
  expectedRunnerPublicKey: string,
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
  if (value.max_frame_bytes !== 65_535 || value.max_session_bytes !== RUNNER_SESSION_BYTES) {
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
  if (relayUrl.protocol !== "wss:" && window.location.hostname !== "localhost") {
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

function parseHandshakeResponse(value: Uint8Array, expectedTransferId: string): void {
  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(value));
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

function parseHealthResponse(value: Uint8Array, requestId: string): RunnerHealth {
  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(value));
  } catch {
    throw new Error("Runner health response is invalid");
  }
  if (payload === null || typeof payload !== "object") {
    throw new Error("Runner health response is invalid");
  }
  const response = payload as Record<string, unknown>;
  const body = response.body;
  if (
    Object.keys(response).length !== 6 ||
    response.v !== 1 ||
    response.type !== "response" ||
    response.sequence !== 0 ||
    response.request_id !== requestId ||
    response.status !== 200 ||
    body === null ||
    typeof body !== "object"
  ) {
    throw new Error("Runner health response did not match the request");
  }
  const health = body as Record<string, unknown>;
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
    api.post<RunnerCapabilityResponse>("/api/settings/runner/capabilities", request),
  openWebSocket: (url) => new WebSocket(url),
  createRequestId: () => crypto.randomUUID(),
};

export async function checkEncryptedRunnerHealth(
  expectedRunnerPublicKey: string,
  dependencies: RunnerClientDependencies = defaultDependencies,
): Promise<RunnerHealth> {
  decodePublicKey(expectedRunnerPublicKey);
  const keypair = await dependencies.createKeypair();
  const clientPublicKey = encodeBase64url(keypair.publicKey);
  const capability = validateCapability(
    await dependencies.issueCapability({
      initiator_public_key: clientPublicKey,
      scopes: ["worker.health"],
      max_session_bytes: RUNNER_SESSION_BYTES,
    }),
    expectedRunnerPublicKey,
  );
  let initiator: NoiseIkInitiator;
  try {
    initiator = await dependencies.createInitiator({
      staticPrivateKey: keypair.privateKey,
      responderStaticPublicKey: decodePublicKey(capability.runner_public_key),
      prologue: new TextEncoder().encode(RUNNER_PROTOCOL),
    });
  } finally {
    keypair.privateKey.fill(0);
  }
  const socket = dependencies.openWebSocket(capability.relay_url);
  socket.binaryType = "arraybuffer";
  try {
    await waitForOpen(socket);
    const ready = await sendAndReceive(socket, capability.capability);
    if (ready !== '{"type":"ready"}') {
      throw new Error("Runner relay did not become ready");
    }
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

    const requestId = dependencies.createRequestId();
    const request = new TextEncoder().encode(
      JSON.stringify({
        body: null,
        method: "GET",
        path: "/health",
        request_id: requestId,
        sequence: 0,
        type: "request",
        v: 1,
      }),
    );
    const encryptedResponse = await sendAndReceive(socket, initiator.encrypt(request));
    if (!(encryptedResponse instanceof ArrayBuffer)) {
      throw new Error("Runner RPC response must be binary");
    }
    return parseHealthResponse(
      initiator.decrypt(new Uint8Array(encryptedResponse)),
      requestId,
    );
  } finally {
    initiator.dispose();
    if (socket.readyState === WebSocket.OPEN) {
      socket.close(1000, "Runner health check complete");
    }
  }
}
