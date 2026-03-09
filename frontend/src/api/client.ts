/* ---------- Types ---------- */

export interface Repo {
  id: string;
  created_at: string;
  updated_at: string;
  name: string;
  remote_url: string | null;
  root_path: string;
  custom_prompt: string | null;
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

/* ---------- HTTP Client ---------- */

class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const opts: RequestInit = {
    method,
    headers: { "Content-Type": "application/json" },
    credentials: "include",
  };
  if (body !== undefined) {
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, text || res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  delete: (path: string) => request<void>("DELETE", path),
};

/* ---------- WebSocket Manager ---------- */

export type WSEvent =
  | { type: "message"; data: { type: "assistant"; message: { content: ContentBlock[] } } }
  | { type: "message"; data: { type: "tool_use"; tool_name: string; input: unknown } }
  | { type: "message"; data: { type: "result"; [key: string]: unknown } }
  | { type: "error"; error: string };

export interface ContentBlock {
  type: string;
  text?: string;
  id?: string;
  name?: string;
  input?: unknown;
}

type EventCallback = (event: WSEvent) => void;

export class AgentSocket {
  private ws: WebSocket | null = null;
  private listeners = new Set<EventCallback>();
  private _connected = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private shouldReconnect = true;

  constructor(private sessionId: string) {}

  get connected(): boolean {
    return this._connected;
  }

  connect(): void {
    this.shouldReconnect = true;
    this.openSocket();
  }

  private openSocket(): void {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/ws/${this.sessionId}`;
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this._connected = true;
      this.emit({ type: "message", data: { type: "result", connected: true } } as WSEvent);
    };

    this.ws.onmessage = (ev) => {
      try {
        const event = JSON.parse(ev.data) as WSEvent;
        this.emit(event);
      } catch {
        /* ignore malformed messages */
      }
    };

    this.ws.onclose = () => {
      this._connected = false;
      if (this.shouldReconnect) {
        this.reconnectTimer = setTimeout(() => this.openSocket(), 2000);
      }
    };

    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  sendPrompt(prompt: string, model?: string): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const msg: { type: string; prompt: string; model?: string } = {
      type: "prompt",
      prompt,
    };
    if (model) msg.model = model;
    this.ws.send(JSON.stringify(msg));
  }

  cancel(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: "cancel" }));
  }

  on(cb: EventCallback): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  private emit(event: WSEvent): void {
    this.listeners.forEach((cb) => cb(event));
  }

  disconnect(): void {
    this.shouldReconnect = false;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
    this._connected = false;
  }
}
