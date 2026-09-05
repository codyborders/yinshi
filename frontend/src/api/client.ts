import { ApiError, apiErrorFromPayload } from "./errors";

export { ApiError, apiErrorFromPayload } from "./errors";

export interface Repo {
  id: string;
  created_at: string;
  updated_at: string;
  name: string;
  remote_url: string | null;
  root_path: string;
  custom_prompt: string | null;
  agents_md: string | null;
}

export interface GitHubInstallation {
  installation_id: number;
  account_login: string;
  account_type: string;
  html_url: string;
}

export interface ApiKey {
  id: string;
  created_at: string;
  provider: string;
  label: string;
  last_used_at: string | null;
}

export interface ProviderSetupField {
  key: string;
  label: string;
  required: boolean;
  secret: boolean;
}

export interface ProviderDescriptor {
  id: string;
  label: string;
  auth_strategies: string[];
  setup_fields: ProviderSetupField[];
  docs_url: string;
  connected: boolean;
  model_count: number;
}

export type ThinkingLevel =
  "off" | "minimal" | "low" | "medium" | "high" | "xhigh";

export interface ModelDescriptor {
  ref: string;
  provider: string;
  id: string;
  label: string;
  api: string;
  reasoning: boolean;
  thinking_levels?: ThinkingLevel[];
  inputs: string[];
  context_window: number;
  max_tokens: number;
}

export interface ProviderCatalog {
  default_model: string;
  providers: ProviderDescriptor[];
  models: ModelDescriptor[];
}

export interface ProviderConnection {
  id: string;
  created_at: string;
  updated_at: string;
  provider: string;
  auth_strategy: string;
  label: string;
  config: Record<string, unknown>;
  status: string;
  last_used_at: string | null;
  expires_at: string | null;
}

export type CloudRunnerStatus = "pending" | "online" | "offline" | "revoked";
export type RunnerStorageProfile =
  "aws_ebs_s3_files" | "archil_shared_files" | "archil_all_posix";

export interface CloudRunner {
  id: string;
  created_at: string;
  updated_at: string;
  name: string;
  cloud_provider: string;
  region: string;
  status: CloudRunnerStatus;
  registered_at: string | null;
  last_heartbeat_at: string | null;
  runner_version: string | null;
  capabilities: Record<string, unknown>;
  data_dir: string | null;
  noise_public_key: string | null;
  noise_key_fingerprint: string | null;
  noise_key_confirmed: boolean;
}

export interface CloudRunnerRegistration {
  runner: CloudRunner;
  registration_token: string;
  registration_token_expires_at: string;
  control_url: string;
  environment: Record<string, string>;
}

export type ProviderAuthorizationMode = "browser" | "device_code";

export interface ProviderAuthStart {
  flow_id: string;
  provider: string;
  auth_url: string;
  authorization_mode?: ProviderAuthorizationMode;
  user_code?: string | null;
  instructions: string | null;
  manual_input_required: boolean;
  manual_input_prompt: string | null;
  manual_input_submitted: boolean;
}

export interface ProviderAuthStatus {
  status: string;
  provider: string;
  flow_id: string;
  authorization_mode?: ProviderAuthorizationMode;
  user_code?: string | null;
  instructions?: string | null;
  progress?: string[];
  manual_input_required?: boolean;
  manual_input_prompt?: string | null;
  manual_input_submitted?: boolean;
  error?: string | null;
}

export interface PiConfig {
  id: string;
  created_at: string;
  updated_at: string;
  source_type: "upload" | "github";
  source_label: string;
  last_synced_at: string | null;
  status: "ready" | "cloning" | "syncing" | "error";
  error_message: string | null;
  available_categories: string[];
  enabled_categories: string[];
}

export type PiCommandKind = "skill" | "prompt" | "extension";

export interface PiCommand {
  kind: PiCommandKind;
  name: string;
  description: string;
  command_name: string;
}

export interface PiConfigCommands {
  commands: PiCommand[];
}

export interface PiPackageRelease {
  tag_name: string;
  version: string;
  name: string;
  published_at: string | null;
  html_url: string;
  body_markdown: string;
}

export interface PiReleaseNotes {
  package_name: string;
  installed_version: string | null;
  latest_version: string | null;
  node_version: string | null;
  release_notes_url: string;
  update_policy: string;
  runtime_error: string | null;
  release_error: string | null;
  releases: PiPackageRelease[];
}

export interface Workspace {
  id: string;
  created_at: string;
  updated_at: string;
  repo_id: string;
  name: string;
  branch: string;
  path: string;
  state: string;
  kind: "primary" | "delegated" | string;
  parent_workspace_id: string | null;
  delegation_id: string | null;
  delegation_status: ThreadDelegationStatus | null;
}

export interface SessionInfo {
  id: string;
  created_at: string;
  updated_at: string;
  workspace_id: string;
  status: string;
  model: string;
  pi_context_version: number;
}

export type ThreadLifecycleStatus =
  | "provisioning"
  | "queued"
  | "running"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type ThreadDelegationStatus = ThreadLifecycleStatus;

export interface ThreadNodeOut {
  id: string;
  delegation_id: string | null;
  parent_id: string | null;
  root_id: string;
  depth: number;
  title: string | null;
  role: string;
  origin: string;
  state: string;
  workspace_id: string;
  model: string;
  child_count: number;
  active_child_count: number;
  can_spawn_child: boolean;
  created_at: string;
}

export type ThreadChildrenOut = ThreadNodeOut[];

export interface ThreadPlaceholderOut {
  delegation_id: string;
  parent_id: string;
  title: string;
  role: string;
  status: string;
  created_at: string;
}

export interface ThreadTreeOut {
  root: ThreadNodeOut;
  nodes: ThreadNodeOut[];
  placeholders: ThreadPlaceholderOut[];
  thread_count: number;
  active_descendant_count: number;
  tree_depth: number;
}

export interface ThreadLimitsOut {
  max_depth: number;
  max_direct_children: number;
  max_active_descendants: number;
  max_total_threads: number;
  tree_depth: number;
  direct_children: number;
  active_descendants: number;
  total_threads: number;
  can_spawn_child: boolean;
}

export interface ThreadSpawnOut {
  delegation_id: string;
  status: ThreadLifecycleStatus;
  child_session_id: string | null;
  error_code: string | null;
}

export interface ThreadResultTest {
  command: string;
  status: "passed" | "failed" | "skipped";
  summary: string | null;
}

export interface ThreadResultOut {
  delegation_id: string;
  version: number;
  source: string;
  sealed: boolean;
  summary: string | null;
  tests: ThreadResultTest[];
  warnings: string[];
  base_commit: string | null;
  result_commit: string | null;
  result_ref: string | null;
  changed_files: unknown[];
  created_at: string;
  updated_at: string;
  sealed_at: string | null;
}

export interface ThreadChildCreate {
  idempotency_key: string;
  title: string;
  task: string;
  context?: string | null;
  role?: "general" | "research" | "implementation" | "test" | "review" | "debug";
  model?: string | null;
  thinking?: ThinkingLevel | null;
  start_immediately?: boolean;
}

export type ThreadCreateBody = ThreadChildCreate;

export interface ThreadRetryCreate {
  idempotency_key: string;
  model?: string | null;
  thinking?: ThinkingLevel | null;
}

export type ThreadRetryBody = ThreadRetryCreate;
export type ThreadCancelBody = undefined;

export interface ThreadResultReportTest {
  command: string;
  status: "passed" | "failed" | "skipped";
  summary?: string | null;
}

export interface ThreadResultReportCreate {
  expected_version: number;
  summary: string;
  tests?: ThreadResultReportTest[];
  warnings?: string[];
}

export type ThreadReportBody = ThreadResultReportCreate;

export interface Message {
  id: string;
  created_at: string;
  session_id: string;
  role: string;
  content: string | null;
  full_message: string | null;
  turn_id: string | null;
  turn_status: string | null;
}

export interface WorkspaceFileNode {
  name: string;
  path: string;
  type: "file" | "directory";
  children: WorkspaceFileNode[];
}

export interface WorkspaceChangedFile {
  path: string;
  status: string;
  kind:
    | "added"
    | "copied"
    | "deleted"
    | "modified"
    | "renamed"
    | "untracked"
    | "unknown";
  original_path: string | null;
}

async function _readApiError(response: Response): Promise<ApiError> {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const payload = await response.json().catch(() => null);
    const error = apiErrorFromPayload(response.status, payload);
    if (payload !== null) {
      return error;
    }
  }

  const text = await response.text().catch(() => "");
  return new ApiError(response.status, text || response.statusText);
}

const DESKTOP_HOSTED_RUNNER_PATHS = new Set([
  "/api/settings/runner",
  "/api/settings/runner/capabilities",
  "/api/settings/runner/noise-key/confirm",
]);

async function desktopHostedRequest<T>(
  method: DesktopHostedApiRequest["method"],
  path: string,
  body: unknown,
  forceHosted = false,
): Promise<T | undefined> {
  const bridge = window.yinshiDesktop;
  if (
    bridge === undefined ||
    (!forceHosted && !DESKTOP_HOSTED_RUNNER_PATHS.has(path))
  ) {
    return undefined;
  }
  if (!["DELETE", "GET", "PATCH", "POST", "PUT"].includes(method)) {
    throw new TypeError("Desktop hosted API method is not supported");
  }
  if (
    body !== undefined &&
    (body === null || typeof body !== "object" || Array.isArray(body))
  ) {
    throw new TypeError("Desktop hosted API body must be an object");
  }
  const hostedRequest: DesktopHostedApiRequest =
    body === undefined
      ? { method, path }
      : { method, path, body: body as Readonly<Record<string, unknown>> };
  const response = await bridge.hostedRequest(hostedRequest);
  if (
    !Number.isInteger(response.status) ||
    response.status < 100 ||
    response.status > 599
  ) {
    throw new Error("Desktop hosted API returned an invalid status");
  }
  if (response.status < 200 || response.status > 299) {
    throw apiErrorFromPayload(response.status, response.body);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return response.body as T;
}

async function request<T>(
  method: DesktopHostedApiRequest["method"],
  path: string,
  body?: unknown,
): Promise<T> {
  const usesDesktopHostedApi =
    window.yinshiDesktop !== undefined && DESKTOP_HOSTED_RUNNER_PATHS.has(path);
  const hostedResponse = await desktopHostedRequest<T>(method, path, body);
  if (usesDesktopHostedApi) {
    return hostedResponse as T;
  }
  const opts: RequestInit = {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
    },
    credentials: "include",
  };
  if (body !== undefined) {
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    if (res.status === 401 && window.location.pathname.startsWith("/app")) {
      window.location.href = "/";
    }
    throw await _readApiError(res);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

async function hostedRequest<T>(
  method: DesktopHostedApiRequest["method"],
  path: string,
  body?: unknown,
): Promise<T> {
  if (window.yinshiDesktop !== undefined) {
    return (await desktopHostedRequest<T>(method, path, body, true)) as T;
  }
  return request<T>(method, path, body);
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  delete: (path: string) => request<void>("DELETE", path),
  upload: async <T>(path: string, file: File): Promise<T> => {
    const form = new FormData();
    form.append("file", file);
    const response = await fetch(path, {
      method: "POST",
      credentials: "include",
      headers: { "X-Requested-With": "XMLHttpRequest" },
      body: form,
    });
    if (!response.ok) {
      if (
        response.status === 401 &&
        window.location.pathname.startsWith("/app")
      ) {
        window.location.href = "/";
      }
      throw await _readApiError(response);
    }
    return response.json();
  },
};

export const hostedApi = {
  get: <T>(path: string) => hostedRequest<T>("GET", path),
  post: <T>(path: string, body?: unknown) =>
    hostedRequest<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) =>
    hostedRequest<T>("PATCH", path, body),
  put: <T>(path: string, body?: unknown) => hostedRequest<T>("PUT", path, body),
  delete: (path: string) => hostedRequest<void>("DELETE", path),
  upload: <T>(path: string, file: File) => api.upload<T>(path, file),
};

export type SSEEvent =
  | { type: "assistant"; message: { content: ContentBlock[] } }
  | {
      type: "tool_use";
      name: string;
      tool_name?: string;
      id?: string;
      input: unknown;
    }
  | {
      type: "tool_result";
      tool_use_id: string;
      content: string | ContentBlock[] | unknown;
      is_error?: boolean;
    }
  | { type: "content_block_start"; content_block: ContentBlock; index?: number }
  | {
      type: "content_block_delta";
      delta: {
        type: string;
        text?: string;
        partial_json?: string;
        thinking?: string;
      };
      index?: number;
    }
  | { type: "content_block_stop"; index?: number }
  | { type: "message_start"; message?: unknown }
  | { type: "message_delta"; delta?: unknown }
  | { type: "message_stop" }
  | { type: "result"; [key: string]: unknown }
  | {
      type: "status";
      status: string;
      message?: string;
      severity?: "info" | "warning" | "error";
      [key: string]: unknown;
    }
  | { type: "cancelled"; reason?: string }
  | { type: "error"; error: string };

export interface ContentBlock {
  type: string;
  text?: string;
  thinking?: string;
  id?: string;
  name?: string;
  input?: unknown;
}

export function normalizeEvent(raw: Record<string, unknown>): SSEEvent {
  if (raw.type === "tool_use") {
    return {
      type: "tool_use",
      name: (raw.toolName || raw.name || raw.tool_name || "unknown") as string,
      id: raw.id as string,
      input: raw.toolInput ?? raw.input,
    };
  }
  return raw as SSEEvent;
}

export async function* streamPrompt(
  sessionId: string,
  prompt: string,
  model?: string,
  thinking?: ThinkingLevel,
  signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const res = await fetch(`/api/sessions/${sessionId}/prompt`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
    },
    credentials: "include",
    body: JSON.stringify({ prompt, model, thinking }),
    signal,
  });

  if (!res.ok) {
    if (res.status === 401 && window.location.pathname.startsWith("/app")) {
      window.location.href = "/";
    }
    throw await _readApiError(res);
  }

  if (!res.body) {
    throw new ApiError(res.status, "Response body is null");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Parse complete SSE lines from buffer
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith("data: ")) {
          try {
            yield normalizeEvent(JSON.parse(trimmed.slice(6)));
          } catch {
            /* ignore malformed events */
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export async function cancelSession(sessionId: string): Promise<void> {
  await request<{ status: string }>(
    "POST",
    `/api/sessions/${sessionId}/cancel`,
  );
}
