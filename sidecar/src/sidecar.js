import net from "node:net";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  createAgentSession,
  AuthStorage,
  ModelRegistry,
  SessionManager,
  SettingsManager,
  createCodingTools,
} from "@mariozechner/pi-coding-agent";
import { getModel } from "@mariozechner/pi-ai";

import { HEALTH_CHECK_INTERVAL } from "./constants.js";

const __sidecarDir = path.dirname(fileURLToPath(import.meta.url));

function sendToSocket(socket, message) {
  if (socket.destroyed) return;
  socket.write(JSON.stringify(message) + "\n");
}

const ANTHROPIC_MODEL_IDS = {
  opus: "claude-opus-4-20250514",
  sonnet: "claude-sonnet-4-20250514",
  haiku: "claude-haiku-4-5-20251001",
};

const DEFAULT_MODEL_KEY = "minimax-m2.7";

function createMinimaxFallbackModel(id, name) {
  return {
    id,
    name,
    api: "openai-completions",
    provider: "minimax",
    baseUrl: "https://api.minimaxi.chat/v1",
    reasoning: false,
    input: ["text"],
    contextWindow: 200000,
    maxTokens: 16384,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  };
}

const MINIMAX_FALLBACK_MODELS = {
  "MiniMax-M2.5-highspeed": createMinimaxFallbackModel(
    "MiniMax-M2.5-highspeed",
    "MiniMax M2.5 Highspeed",
  ),
  "MiniMax-M2.7": createMinimaxFallbackModel(
    "MiniMax-M2.7",
    "MiniMax M2.7",
  ),
  "MiniMax-M2.7-highspeed": createMinimaxFallbackModel(
    "MiniMax-M2.7-highspeed",
    "MiniMax M2.7 Highspeed",
  ),
};

const MINIMAX_MODEL_IDS_BY_KEY = {
  minimax: "MiniMax-M2.7",
  "minimax-m2.7": "MiniMax-M2.7",
  "minimax-m2.7-highspeed": "MiniMax-M2.7-highspeed",
  "minimax-m2.5-highspeed": "MiniMax-M2.5-highspeed",
};

function resolveMinimaxModel(modelKey) {
  if (typeof modelKey !== "string") {
    return null;
  }

  const normalizedKey = modelKey.trim().toLowerCase();
  if (!normalizedKey) {
    return null;
  }

  const modelId = MINIMAX_MODEL_IDS_BY_KEY[normalizedKey];
  if (!modelId) {
    return null;
  }

  const model = getModel("minimax", modelId);
  return { model: model || MINIMAX_FALLBACK_MODELS[modelId] };
}

function resolveModel(modelKey) {
  if (ANTHROPIC_MODEL_IDS[modelKey]) {
    const model = getModel("anthropic", ANTHROPIC_MODEL_IDS[modelKey]);
    return model ? { model } : null;
  }

  const minimaxModel = resolveMinimaxModel(modelKey);
  if (minimaxModel) {
    return minimaxModel;
  }

  return null;
}

export class YinshiSidecar {
  constructor() {
    this.activeSessions = new Map();
    this.socketPath = process.env.SIDECAR_SOCKET_PATH || "/tmp/yinshi-sidecar.sock";
    this.server = net.createServer((socket) => this.handleConnection(socket));
    this.healthCheckInterval = null;

    process.on("SIGINT", () => this.cleanup());
    process.on("SIGTERM", () => this.cleanup());
  }

  initialize() {
    // API keys come per-session from the backend via the socket protocol.
    // In containerized mode there is no .env file in the image.
    if (process.env.SIDECAR_LOAD_DOTENV === "1") {
      this._loadDotEnv();
    }
    console.log(`[sidecar] Initialized with pi SDK`);
  }

  _loadDotEnv() {
    const envPath = path.join(__sidecarDir, "..", "..", ".env");
    if (!fs.existsSync(envPath)) {
      console.log("[sidecar] No .env file found, skipping");
      return;
    }
    const content = fs.readFileSync(envPath, "utf-8");
    for (const line of content.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const eqIndex = trimmed.indexOf("=");
      if (eqIndex === -1) continue;
      const key = trimmed.slice(0, eqIndex).trim();
      const value = trimmed.slice(eqIndex + 1).trim();
      if (!process.env[key]) {
        process.env[key] = value;
        console.log(`[sidecar] Loaded env: ${key}=***`);
      }
    }
  }

  async start() {
    this.cleanup();

    return new Promise((resolve, reject) => {
      this.server.listen(this.socketPath, () => {
        console.log(`SOCKET_PATH=${this.socketPath}`);
        this.healthCheckInterval = setInterval(() => {
          console.log(`[sidecar] Health: ${this.activeSessions.size} session(s)`);
        }, HEALTH_CHECK_INTERVAL);
        resolve();
      });
      this.server.on("error", (err) => {
        console.error("[sidecar] Server error:", err.message);
        reject(err);
      });
    });
  }

  handleConnection(socket) {
    console.log("[sidecar] New connection");
    sendToSocket(socket, { id: "init", type: "init_status", success: true });

    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk.toString();
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.length === 0) continue;
        this.handleData(trimmed, socket);
      }
    });
    socket.on("error", (err) => console.error("[sidecar] Socket error:", err.message));
    socket.on("close", () => console.log("[sidecar] Connection closed"));
  }

  handleData(data, socket) {
    let parsed;
    try {
      parsed = JSON.parse(data);
    } catch (err) {
      sendToSocket(socket, { id: "unknown", type: "error", error: `Parse error: ${err.message}` });
      return;
    }
    this.handleRequest(parsed, socket);
  }

  handleRequest(request, socket) {
    const { type, id } = request;
    switch (type) {
      case "query":
        this.processQuery(id, socket, request.prompt, request.options || {});
        break;
      case "cancel":
        this.cancelSession(id);
        break;
      case "warmup":
        this.warmupSession(id, socket, request.options || {});
        break;
      case "resolve":
        this.handleResolve(id, socket, request.model);
        break;
      case "ping":
        sendToSocket(socket, { type: "pong" });
        break;
      default:
        sendToSocket(socket, { id: id || "unknown", type: "error", error: `Unknown request type: ${type}` });
    }
  }

  handleResolve(id, socket, modelKey) {
    if (!modelKey) {
      sendToSocket(socket, { id: id || "unknown", type: "error", error: "Model key required" });
      return;
    }
    const resolved = resolveModel(modelKey);
    if (!resolved) {
      sendToSocket(socket, { id: id || "unknown", type: "error", error: `Unknown model: ${modelKey}` });
      return;
    }
    sendToSocket(socket, {
      id,
      type: "resolved",
      provider: resolved.model.provider,
      model: resolved.model.id,
    });
  }

  _normalizeImportedSettings(importedSettings) {
    if (importedSettings === null || importedSettings === undefined) {
      return null;
    }
    if (typeof importedSettings !== "object" || Array.isArray(importedSettings)) {
      throw new Error("Imported settings must be an object");
    }
    return importedSettings;
  }

  async _createPiSession(
    sessionId,
    modelKey,
    cwd,
    userApiKey = null,
    agentDir = null,
    importedSettings = null,
  ) {
    const resolved = resolveModel(modelKey);
    if (!resolved) {
      throw new Error(`Unknown model: ${modelKey}`);
    }

    const { model } = resolved;

    // Per-session auth storage to prevent key leakage between concurrent sessions
    const sessionAuth = AuthStorage.create();
    const sessionRegistry = new ModelRegistry(sessionAuth);

    // BYOK key from backend > env var fallback (for dev mode)
    const envKey = `${model.provider.toUpperCase()}_API_KEY`;
    const effectiveKey = userApiKey || process.env[envKey] || null;
    if (effectiveKey) {
      sessionAuth.setRuntimeApiKey(model.provider, effectiveKey);
    }

    const settingsManager = SettingsManager.inMemory({
      compaction: { enabled: true },
      retry: { enabled: true, maxRetries: 3 },
    });
    const normalizedImportedSettings = this._normalizeImportedSettings(importedSettings);
    if (normalizedImportedSettings) {
      settingsManager.applyOverrides(normalizedImportedSettings);
    }

    const sessionOptions = {
      cwd,
      model,
      thinkingLevel: "off",
      tools: createCodingTools(cwd),
      sessionManager: SessionManager.inMemory(),
      settingsManager,
      authStorage: sessionAuth,
      modelRegistry: sessionRegistry,
    };
    if (agentDir) {
      sessionOptions.agentDir = agentDir;
    }

    const { session } = await createAgentSession(sessionOptions);

    console.log(
      `[sidecar] Created pi session ${sessionId} with model ${model.name || model.id}`
      + (agentDir ? ` and agentDir ${agentDir}` : ""),
    );
    return { session, model };
  }

  async warmupSession(sessionId, socket, options) {
    if (this.activeSessions.has(sessionId)) {
      console.log(`[sidecar] Session ${sessionId} already exists`);
      return;
    }

    const modelKey = options.model || DEFAULT_MODEL_KEY;
    const cwd = options.cwd || process.cwd();
    const userApiKey = options.apiKey || null;
    const agentDir = options.agentDir || null;
    const importedSettings = options.settings || null;

    try {
      const { session: piSession, model } = await this._createPiSession(
        sessionId,
        modelKey,
        cwd,
        userApiKey,
        agentDir,
        importedSettings,
      );
      this.activeSessions.set(sessionId, {
        piSession,
        model,
        modelKey,
        cwd,
        unsubscribe: null,
      });
      console.log(`[sidecar] Warmed up session ${sessionId}`);
    } catch (err) {
      console.error(`[sidecar] Warmup failed: ${err.message}`);
      sendToSocket(socket, { id: sessionId, type: "error", error: err.message });
    }
  }

  async processQuery(sessionId, socket, prompt, options) {
    const modelKey = options.model || DEFAULT_MODEL_KEY;
    const cwd = options.cwd || process.cwd();
    const userApiKey = options.apiKey || null;
    const agentDir = options.agentDir || null;
    const importedSettings = options.settings || null;

    try {
      let entry = this.activeSessions.get(sessionId);

      if (!entry || entry.modelKey !== modelKey) {
        if (entry) {
          if (entry.unsubscribe) entry.unsubscribe();
          entry.piSession.dispose();
        }
        const { session: piSession, model } = await this._createPiSession(
          sessionId,
          modelKey,
          cwd,
          userApiKey,
          agentDir,
          importedSettings,
        );
        entry = { piSession, model, modelKey, cwd, unsubscribe: null };
        this.activeSessions.set(sessionId, entry);
      }

      const { piSession, model } = entry;

      if (entry.unsubscribe) {
        entry.unsubscribe();
      }

      let usage = null;

      entry.unsubscribe = piSession.subscribe((event) => {
        switch (event.type) {
          case "message_update": {
            const ame = event.assistantMessageEvent;
            if (ame.type === "text_delta") {
              sendToSocket(socket, {
                id: sessionId,
                type: "message",
                data: {
                  type: "assistant",
                  message: {
                    content: [{ type: "text", text: ame.delta }],
                  },
                },
              });
            }
            break;
          }

          case "tool_execution_start":
            sendToSocket(socket, {
              id: sessionId,
              type: "message",
              data: {
                type: "tool_use",
                toolName: event.toolName,
                toolInput: event.args,
              },
            });
            break;

          case "tool_execution_end":
            break;

          case "turn_end":
            if (event.message && event.message.usage) {
              const u = event.message.usage;
              usage = {
                input_tokens: u.input || 0,
                output_tokens: u.output || 0,
                cache_read_input_tokens: u.cacheRead || 0,
                cache_creation_input_tokens: u.cacheWrite || 0,
              };
            }
            break;

          case "agent_end":
            sendToSocket(socket, {
              id: sessionId,
              type: "message",
              data: {
                type: "result",
                usage: usage || {},
                provider: model.provider,
              },
            });
            usage = null;
            break;

          case "auto_retry_start":
            console.log(`[sidecar] Retrying (attempt ${event.attempt}/${event.maxAttempts}): ${event.errorMessage}`);
            break;

          case "auto_compaction_start":
            console.log(`[sidecar] Auto-compacting context...`);
            break;
        }
      });

      await piSession.prompt(prompt);
    } catch (err) {
      console.error(`[sidecar] Error in session ${sessionId}:`, err.message);
      sendToSocket(socket, {
        id: sessionId,
        type: "error",
        error: err.message,
      });
    }
  }

  async cancelSession(sessionId) {
    const entry = this.activeSessions.get(sessionId);
    if (!entry) {
      console.log(`[sidecar] Session ${sessionId} not found`);
      return;
    }
    console.log(`[sidecar] Cancelling session ${sessionId}`);
    await entry.piSession.abort();
  }

  cleanup() {
    try {
      if (fs.existsSync(this.socketPath)) {
        fs.unlinkSync(this.socketPath);
      }
    } catch (_) {}

    if (this.server) {
      try { this.server.close(); } catch (_) {}
    }

    for (const [id, entry] of this.activeSessions) {
      try {
        if (entry.unsubscribe) entry.unsubscribe();
        entry.piSession.dispose();
      } catch (_) {}
    }
    this.activeSessions.clear();

    if (this.healthCheckInterval) {
      clearInterval(this.healthCheckInterval);
      this.healthCheckInterval = null;
    }
  }
}
