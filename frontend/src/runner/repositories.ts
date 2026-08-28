import type { Repo } from "../api/client";
import { requestEncryptedRunner } from "./encryptedRunnerClient";

export interface RunnerRepositoryTarget {
  readonly runnerId: string;
  readonly runnerPublicKey: string;
}

function validateRepository(value: unknown): Repo {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Runner repository response is invalid");
  }
  const repo = value as Record<string, unknown>;
  const requiredStrings = ["id", "created_at", "updated_at", "name", "root_path"];
  if (requiredStrings.some((field) => typeof repo[field] !== "string" || !repo[field])) {
    throw new Error("Runner repository response is missing required fields");
  }
  for (const field of ["remote_url", "custom_prompt", "agents_md"]) {
    if (repo[field] !== null && typeof repo[field] !== "string") {
      throw new Error("Runner repository response has an invalid optional field");
    }
  }
  return repo as unknown as Repo;
}

function validateRepositoryName(value: string): string {
  if (typeof value !== "string") {
    throw new TypeError("Repository name must be a string");
  }
  const name = value.trim();
  if (!name || name.length > 255 || name !== value || /[\\/]/.test(name) || name === "." || name === "..") {
    throw new Error("Repository name must be a simple name");
  }
  return name;
}

function validateRepositoryUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("Runner repository URL must be valid HTTPS");
  }
  if (url.protocol !== "https:" || !url.hostname) {
    throw new Error("Runner repository URL must use HTTPS");
  }
  if (url.username || url.password) {
    throw new Error("Runner repository URL must not include embedded credentials");
  }
  if (url.search || url.hash) {
    throw new Error("Runner repository URL must not include a query or fragment");
  }
  return url.toString();
}

function requestRunnerRepository<T>(
  target: RunnerRepositoryTarget,
  scope: "repository.read" | "repository.write",
  method: "GET" | "POST",
  body: unknown,
): Promise<T> {
  if (!target.runnerId || !target.runnerPublicKey) {
    throw new Error("Runner repository target is incomplete");
  }
  if (target.runnerId.length > 256) {
    throw new Error("BYOC runner ID is invalid");
  }
  return requestEncryptedRunner<T>({
    expectedRunnerPublicKey: target.runnerPublicKey,
    scopes: [scope],
    method,
    path: "/api/repos",
    query: {},
    body,
    maxSessionBytes: 16 * 1024 * 1024,
  });
}

export async function listRunnerRepositories(
  target: RunnerRepositoryTarget,
): Promise<Repo[]> {
  const response = await requestRunnerRepository<unknown>(
    target,
    "repository.read",
    "GET",
    null,
  );
  if (!Array.isArray(response)) {
    throw new Error("Runner repository list response is invalid");
  }
  return response.map(validateRepository);
}

export async function importRunnerRepository(
  target: RunnerRepositoryTarget,
  nameValue: string,
  remoteUrlValue: string,
): Promise<Repo> {
  const name = validateRepositoryName(nameValue);
  const remoteUrl = validateRepositoryUrl(remoteUrlValue);
  const response = await requestRunnerRepository<unknown>(
    target,
    "repository.write",
    "POST",
    {
      name,
      remote_url: remoteUrl,
    },
  );
  return validateRepository(response);
}
