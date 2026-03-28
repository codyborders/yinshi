export interface Repo {
  id: string;
  created_at: string;
  updated_at: string;
  name: string;
  remote_url: string | null;
  root_path: string;
  custom_prompt: string | null;
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

export interface ModelDescriptor {
  ref: string;
  provider: string;
  id: string;
  label: string;
  api: string;
  reasoning: boolean;
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

export interface ProviderAuthStart {
  flow_id: string;
  provider: string;
  auth_url: string;
  instructions: string | null;
  manual_input_required: boolean;
  manual_input_prompt: string | null;
  manual_input_submitted: boolean;
}

export interface ProviderAuthStatus {
  status: string;
  provider: string;
  flow_id: string;
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

export interface Workspace {
  id: string;
  created_at: string;
  updated_at: string;
  repo_id: string;
  name: string;
  branch: string;
  path: string;
  state: string;
}

export interface SessionInfo {
  id: string;
  created_at: string;
  updated_at: string;
  workspace_id: string;
  status: string;
  model: string;
}

export interface Message {
  id: string;
  created_at: string;
  session_id: string;
  role: string;
  content: string | null;
  full_message: string | null;
  turn_id: string | null;
}

interface StructuredApiErrorPayload {
  code?: string;
  message?: string;
  connect_url?: string | null;
  manage_url?: string | null;
}

export class ApiError extends Error {
  public code?: string;
  public connectUrl?: string | null;
  public manageUrl?: string | null;

  constructor(
    public status: number,
    message: string,
    payload?: StructuredApiErrorPayload,
  ) {
    super(message);
    this.name = "ApiError";
    this.code = payload?.code;
    this.connectUrl = payload?.connect_url;
    this.manageUrl = payload?.manage_url;
  }
}

function _normalizeErrorPayload(payload: unknown): StructuredApiErrorPayload | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const detail = "detail" in payload ? payload.detail : payload;
  if (!detail || typeof detail !== "object") {
    return null;
  }

  const candidate = detail as Record<string, unknown>;
  const normalized: StructuredApiErrorPayload = {};
  if (typeof candidate.code === "string") {
    normalized.code = candidate.code;
  }
  if (typeof candidate.message === "string") {
    normalized.message = candidate.message;
  }
  if (typeof candidate.connect_url === "string") {
    normalized.connect_url = candidate.connect_url;
  }
  if (candidate.connect_url === null) {
    normalized.connect_url = null;
  }
  if (typeof candidate.manage_url === "string") {
    normalized.manage_url = candidate.manage_url;
  }
  if (candidate.manage_url === null) {
    normalized.manage_url = null;
  }
  return normalized;
}

async function _readApiError(response: Response): Promise<ApiError> {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const payload = await response.json().catch(() => null);
    const normalized = _normalizeErrorPayload(payload);
    if (normalized?.message) {
      return new ApiError(response.status, normalized.message, normalized);
    }
    if (payload && typeof payload === "object" && "detail" in payload && typeof payload.detail === "string") {
      return new ApiError(response.status, payload.detail);
    }
  }

  const text = await response.text().catch(() => "");
  return new ApiError(response.status, text || response.statusText);
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const opts: RequestInit = {
    method,
    headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
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

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
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
      if (response.status === 401 && window.location.pathname.startsWith("/app")) {
        window.location.href = "/";
      }
      throw await _readApiError(response);
    }
    return response.json();
  },
};

export async function pollAuthFlow(provider: string, flowId: string): Promise<ProviderAuthStatus> {
  return request<ProviderAuthStatus>(
    "GET",
    `/auth/providers/${provider}/callback?flow_id=${encodeURIComponent(flowId)}`,
  );
}

export async function submitAuthFlowInput(
  provider: string,
  flowId: string,
  authorizationInput: string,
): Promise<ProviderAuthStatus> {
  return request<ProviderAuthStatus>(
    "POST",
    `/auth/providers/${provider}/callback`,
    {
      flow_id: flowId,
      authorization_input: authorizationInput,
    },
  );
}

export type SSEEvent =
  | { type: "assistant"; message: { content: ContentBlock[] } }
  | { type: "tool_use"; name: string; tool_name?: string; id?: string; input: unknown }
  | { type: "tool_result"; tool_use_id: string; content: string | ContentBlock[] | unknown; is_error?: boolean }
  | { type: "content_block_start"; content_block: ContentBlock; index?: number }
  | { type: "content_block_delta"; delta: { type: string; text?: string; partial_json?: string; thinking?: string }; index?: number }
  | { type: "content_block_stop"; index?: number }
  | { type: "message_start"; message?: unknown }
  | { type: "message_delta"; delta?: unknown }
  | { type: "message_stop" }
  | { type: "result"; [key: string]: unknown }
  | { type: "error"; error: string };

export interface ContentBlock {
  type: string;
  text?: string;
  thinking?: string;
  id?: string;
  name?: string;
  input?: unknown;
}

function normalizeEvent(raw: Record<string, unknown>): SSEEvent {
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
  signal?: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const res = await fetch(`/api/sessions/${sessionId}/prompt`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
    credentials: "include",
    body: JSON.stringify({ prompt, model }),
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
  await request<{ status: string }>("POST", `/api/sessions/${sessionId}/cancel`);
}
