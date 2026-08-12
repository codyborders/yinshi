import {
  connectEncryptedRunner,
  RunnerRpcError,
  type EncryptedRunnerConnection,
  type EncryptedRunnerOperation,
} from "../runner/encryptedRunnerClient";
import type { RuntimeRef } from "./runtimeTransport";

const FILE_BYTES_MAX = 50 * 1024 * 1024;
const CHUNK_BYTES = 24_000;
const SESSION_BYTES = 128 * 1024 * 1024;
const RESOURCE_ID_PATTERN = /^[0-9a-f]{32}$/;

interface UploadProgress {
  readonly id: string;
  readonly purpose: "pi_config";
  readonly filename: string;
  readonly size_bytes: number;
  readonly next_chunk_index: number;
}

function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/gu, "-").replace(/\//gu, "_").replace(/=+$/u, "");
}

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function validateProgress(
  value: unknown,
  expected: {
    readonly id?: string;
    readonly filename: string;
    readonly sizeBytes: number;
    readonly nextChunkIndex: number;
  },
): UploadProgress {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Encrypted upload response is invalid");
  }
  const progress = value as Record<string, unknown>;
  if (
    Object.keys(progress).length !== 5 ||
    typeof progress.id !== "string" ||
    !RESOURCE_ID_PATTERN.test(progress.id) ||
    (expected.id !== undefined && progress.id !== expected.id) ||
    progress.purpose !== "pi_config" ||
    progress.filename !== expected.filename ||
    progress.size_bytes !== expected.sizeBytes ||
    progress.next_chunk_index !== expected.nextChunkIndex
  ) {
    throw new Error("Encrypted upload response did not match the transfer");
  }
  return progress as unknown as UploadProgress;
}

function validateFile(file: File): void {
  if (!file || typeof file.name !== "string" || typeof file.size !== "number") {
    throw new TypeError("Pi config upload must be a file");
  }
  if (
    !file.name ||
    file.name.length > 255 ||
    file.name !== file.name.trim() ||
    file.name.includes("/") ||
    file.name.includes("\\") ||
    !file.name.toLowerCase().endsWith(".zip")
  ) {
    throw new Error("Pi config upload must have a simple .zip filename");
  }
  if (!Number.isSafeInteger(file.size) || file.size < 1 || file.size > FILE_BYTES_MAX) {
    throw new Error("Pi config upload must be between 1 byte and 50MB");
  }
}

type UploadInvoker = <Result>(
  operation: EncryptedRunnerOperation,
  retryConnectionFailures?: boolean,
) => Promise<Result>;

async function uploadPiConfigChunks<T>(file: File, invoke: UploadInvoker): Promise<T> {
  validateFile(file);
  const fileBytes = new Uint8Array(await file.arrayBuffer());
  if (fileBytes.length !== file.size) {
    fileBytes.fill(0);
    throw new Error("Pi config file size changed while reading");
  }
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", fileBytes));
  let uploadId: string | null = null;
  try {
    const started = validateProgress(
      await invoke({
        method: "POST",
        path: "/api/settings/pi-config/uploads",
        body: {
          purpose: "pi_config",
          filename: file.name,
          size_bytes: fileBytes.length,
          sha256: hex(digest),
        },
      }),
      {
        filename: file.name,
        sizeBytes: fileBytes.length,
        nextChunkIndex: 0,
      },
    );
    uploadId = started.id;
    const chunkCount = Math.ceil(fileBytes.length / CHUNK_BYTES);
    for (let chunkIndex = 0; chunkIndex < chunkCount; chunkIndex += 1) {
      const offset = chunkIndex * CHUNK_BYTES;
      const chunk = fileBytes.subarray(offset, Math.min(offset + CHUNK_BYTES, fileBytes.length));
      validateProgress(
        await invoke(
          {
            method: "POST",
            path: `/api/settings/pi-config/uploads/${uploadId}/chunks/${chunkIndex}`,
            body: { data: base64url(chunk) },
          },
          true,
        ),
        {
          id: uploadId,
          filename: file.name,
          sizeBytes: fileBytes.length,
          nextChunkIndex: chunkIndex + 1,
        },
      );
    }
    return await invoke<T>({
      method: "POST",
      path: `/api/settings/pi-config/uploads/${uploadId}/complete`,
    });
  } catch (error) {
    if (uploadId) {
      try {
        await invoke({
          method: "DELETE",
          path: `/api/settings/pi-config/uploads/${uploadId}`,
        });
      } catch {
        // Incomplete uploads expire from runtime memory after a bounded interval.
      }
    }
    throw error;
  } finally {
    digest.fill(0);
    fileBytes.fill(0);
  }
}

export async function uploadEncryptedPiConfig<T>(
  runtime: Extract<RuntimeRef, { location: "byoc" | "managed" }>,
  file: File,
): Promise<T> {
  if (
    (runtime.location !== "byoc" && runtime.location !== "managed") ||
    !runtime.runnerPublicKey
  ) {
    throw new Error("Encrypted upload requires a remote runtime");
  }
  const connectionState: { current: EncryptedRunnerConnection | null } = {
    current: null,
  };
  const connect = async (): Promise<EncryptedRunnerConnection> =>
    connectEncryptedRunner({
      expectedRunnerPublicKey: runtime.runnerPublicKey,
      scopes: ["pi.configure"],
      maxSessionBytes: SESSION_BYTES,
      ...(runtime.location === "managed"
        ? { capabilityEndpoint: "/api/runtime/capabilities" as const }
        : {}),
    });
  const invoke: UploadInvoker = async <Result>(
    operation: EncryptedRunnerOperation,
    retryConnectionFailures = false,
  ): Promise<Result> => {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      connectionState.current ??= await connect();
      try {
        return await connectionState.current.request<Result>(operation);
      } catch (error) {
        if (error instanceof RunnerRpcError) throw error;
        connectionState.current.close();
        connectionState.current = null;
        if (!retryConnectionFailures || attempt === 4) throw error;
      }
    }
    throw new Error("Encrypted upload retry limit reached");
  };
  try {
    return await uploadPiConfigChunks<T>(file, invoke);
  } finally {
    connectionState.current?.close();
  }
}

export async function uploadHostedPiConfig<T>(file: File): Promise<T> {
  const bridge = window.yinshiDesktop;
  if (!bridge) throw new Error("Hosted desktop API is unavailable");
  const invoke: UploadInvoker = async <Result>(operation: EncryptedRunnerOperation) => {
    const requestBody = operation.body;
    if (
      requestBody !== undefined &&
      (requestBody === null || typeof requestBody !== "object" || Array.isArray(requestBody))
    ) {
      throw new TypeError("Hosted upload request body must be an object");
    }
    const response = await bridge.hostedRequest(
      requestBody === undefined
        ? { method: operation.method, path: operation.path }
        : {
            method: operation.method,
            path: operation.path,
            body: requestBody as Readonly<Record<string, unknown>>,
          },
    );
    if (response.status < 200 || response.status > 299) {
      const error = new Error(`Hosted upload failed with status ${response.status}`);
      Object.assign(error, { status: response.status, body: response.body });
      throw error;
    }
    return response.body as Result;
  };
  return uploadPiConfigChunks<T>(file, invoke);
}
