import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import * as pty from "node-pty";

import {
  createAgentSession,
  DefaultResourceLoader,
  ModelRegistry,
  ModelRuntime,
  SessionManager,
  SettingsManager,
  createEditTool,
  createReadTool,
  createWriteTool,
} from "@earendil-works/pi-coding-agent";
import {
  getSupportedThinkingLevels,
  InMemoryCredentialStore,
} from "@earendil-works/pi-ai";

import { HEALTH_CHECK_INTERVAL } from "./constants.js";
import { createGitAwareBashTool } from "./git_auth.js";
import { createOrchestrationRpc, THREAD_OPERATIONS } from "./orchestration_rpc.js";
import { createThreadBridgePingTool, createThreadTools } from "./orchestration_tools.js";

const __sidecarDir = path.dirname(fileURLToPath(import.meta.url));
const PI_PACKAGE_NAME = "@earendil-works/pi-coding-agent";
const DEFAULT_MODEL_REF = "minimax/MiniMax-M2.7";
const DEFAULT_THINKING_LEVEL = "medium";
const OFF_THINKING_LEVEL = "off";
const STANDARD_THINKING_LEVELS = ["off", "minimal", "low", "medium", "high"];
const XHIGH_THINKING_LEVELS = [...STANDARD_THINKING_LEVELS, "xhigh"];
const THINKING_LEVELS = new Set(XHIGH_THINKING_LEVELS);
const OAUTH_FLOW_COUNT_MAX = 8;
const OAUTH_FLOW_TTL_MS = 30 * 60 * 1000;
const PI_SESSION_IDLE_TTL_MS = 30 * 60 * 1000;
const PI_SESSION_COUNT_MAX = 16;
const LEGACY_MODEL_ALIASES = new Map([
  ["haiku", "anthropic/claude-haiku-4-5-20251001"],
  ["minimax", DEFAULT_MODEL_REF],
  ["minimax-m2.5-highspeed", "minimax/MiniMax-M2.5-highspeed"],
  ["minimax-m2.7", DEFAULT_MODEL_REF],
  ["minimax-m2.7-highspeed", "minimax/MiniMax-M2.7-highspeed"],
  ["opus", "anthropic/claude-opus-4-20250514"],
  ["sonnet", "anthropic/claude-sonnet-4-20250514"],
]);
const TERMINAL_ENVIRONMENT_KEYS = [
  "HOME",
  "LANG",
  "LC_ALL",
  "LOGNAME",
  "NPM_CONFIG_PREFIX",
  "PATH",
  "PIPX_BIN_DIR",
  "PIPX_HOME",
  "TZ",
  "USER",
  "YINSHI_WORKSPACE_ID",
];

function sendToSocket(socket, message) {
  if (socket.destroyed) {
    return;
  }
  socket.write(`${JSON.stringify(message)}\n`);
}

function sendStatusToSocket(
  socket,
  sessionId,
  status,
  message,
  severity = "info",
  extra = {},
) {
  sendToSocket(socket, {
    id: sessionId,
    type: "status",
    status,
    severity,
    message,
    ...extra,
  });
}

function normalizePositiveInteger(value, fallback, minValue, maxValue) {
  const numericValue = Number(value);
  if (!Number.isFinite(numericValue)) {
    return fallback;
  }
  const integerValue = Math.trunc(numericValue);
  if (integerValue < minValue) {
    return minValue;
  }
  if (integerValue > maxValue) {
    return maxValue;
  }
  return integerValue;
}

function normalizeTerminalId(value) {
  if (typeof value !== "string") {
    throw new Error("terminal workspaceId must be a string");
  }
  const normalized = value.trim();
  if (!/^[0-9a-f]{32}$/.test(normalized)) {
    throw new Error("terminal workspaceId must be a 32-character hex string");
  }
  return normalized;
}

function normalizeTerminalCwd(value) {
  if (typeof value !== "string") {
    throw new Error("terminal cwd must be a string");
  }
  const normalized = value.trim();
  if (!path.isAbsolute(normalized)) {
    throw new Error("terminal cwd must be absolute");
  }
  if (!fs.existsSync(normalized)) {
    throw new Error("terminal cwd does not exist");
  }
  return normalized;
}

export function buildTerminalEnvironment(cwd, shell) {
  const terminalEnvironment = {};
  for (const key of TERMINAL_ENVIRONMENT_KEYS) {
    if (process.env[key]) {
      terminalEnvironment[key] = process.env[key];
    }
  }
  terminalEnvironment.HOME = terminalEnvironment.HOME || "/home/yinshi";
  terminalEnvironment.PATH = terminalEnvironment.PATH || "/usr/local/bin:/usr/bin:/bin";
  terminalEnvironment.SHELL = shell;
  terminalEnvironment.TERM = "xterm-256color";
  terminalEnvironment.COLORTERM = "truecolor";
  terminalEnvironment.PWD = cwd;
  return terminalEnvironment;
}

function appendTerminalScrollback(entry, data) {
  if (typeof data !== "string" || data.length === 0) {
    return;
  }
  entry.scrollback += data;
  if (entry.scrollback.length > entry.scrollbackLimitBytes) {
    entry.scrollback = entry.scrollback.slice(-entry.scrollbackLimitBytes);
  }
}

function sendTerminalError(socket, id, err, fallback) {
  sendToSocket(socket, {
    id: id || "terminal",
    type: "error",
    error: err instanceof Error ? err.message : fallback,
  });
}

function normalizeImportedSettings(importedSettings) {
  if (importedSettings === null || importedSettings === undefined) {
    return null;
  }
  if (typeof importedSettings !== "object" || Array.isArray(importedSettings)) {
    throw new Error("Imported settings must be an object");
  }

  const normalizedSettings = { ...importedSettings };
  if (Object.prototype.hasOwnProperty.call(normalizedSettings, "thinking")) {
    const thinkingOverride = normalizedSettings.thinking;
    if (typeof thinkingOverride !== "boolean") {
      throw new Error("Imported thinking override must be a boolean");
    }
    delete normalizedSettings.thinking;
    if (thinkingOverride) {
      const requestedLevel = normalizedSettings.defaultThinkingLevel;
      if (
        !THINKING_LEVELS.has(requestedLevel)
        || requestedLevel === OFF_THINKING_LEVEL
      ) {
        normalizedSettings.defaultThinkingLevel = DEFAULT_THINKING_LEVEL;
      }
    } else {
      normalizedSettings.defaultThinkingLevel = OFF_THINKING_LEVEL;
    }
  }
  return normalizedSettings;
}

function normalizePiSessionFile(piSessionFile) {
  if (piSessionFile === null || piSessionFile === undefined) {
    return null;
  }
  if (typeof piSessionFile !== "string") {
    throw new Error("piSessionFile must be a string");
  }
  const normalizedPath = piSessionFile.trim();
  if (!normalizedPath) {
    throw new Error("piSessionFile must not be empty");
  }
  if (normalizedPath.includes("\0")) {
    throw new Error("piSessionFile must not contain NUL");
  }
  return normalizedPath;
}

function openSessionManager(cwd, normalizedSessionFile) {
  if (!normalizedSessionFile) {
    return {
      sessionManager: SessionManager.inMemory(),
      resetWarning: null,
      piSessionFile: null,
    };
  }

  const sessionDir = path.dirname(normalizedSessionFile);
  fs.mkdirSync(sessionDir, { recursive: true, mode: 0o700 });
  try {
    return {
      sessionManager: SessionManager.open(normalizedSessionFile, sessionDir, cwd),
      resetWarning: null,
      piSessionFile: normalizedSessionFile,
    };
  } catch {
    const suffix = new Date().toISOString().replace(/[:.]/g, "-");
    const corruptPath = `${normalizedSessionFile}.corrupt-${suffix}`;
    if (fs.existsSync(normalizedSessionFile)) {
      fs.renameSync(normalizedSessionFile, corruptPath);
    }
    return {
      sessionManager: SessionManager.open(normalizedSessionFile, sessionDir, cwd),
      resetWarning:
        "Pi session context was missing or unreadable, so Yinshi started a fresh model context. The visible transcript is still preserved.",
      piSessionFile: normalizedSessionFile,
    };
  }
}

function stringifyToolContent(content) {
  if (typeof content === "string") {
    return content;
  }
  if (!content || typeof content !== "object") {
    return String(content ?? "");
  }
  if (content.type === "text" && typeof content.text === "string") {
    return content.text;
  }
  if (content.type === "image") {
    return "[image]";
  }
  return JSON.stringify(content);
}

function stringifyToolResult(result) {
  if (result === null || result === undefined) {
    return "";
  }
  if (typeof result === "string") {
    return result;
  }
  if (typeof result !== "object") {
    return String(result);
  }
  if (Array.isArray(result.content)) {
    return result.content.map(stringifyToolContent).filter(Boolean).join("\n");
  }
  return JSON.stringify(result, null, 2);
}

function normalizeModelLookup(modelKey) {
  if (typeof modelKey !== "string") {
    return "";
  }
  const trimmedKey = modelKey.trim();
  if (!trimmedKey) {
    return "";
  }
  const normalizedKey = trimmedKey.toLowerCase();
  if (LEGACY_MODEL_ALIASES.has(normalizedKey)) {
    return LEGACY_MODEL_ALIASES.get(normalizedKey) || "";
  }
  return trimmedKey;
}

function buildModelsJsonPath(agentDir) {
  if (!agentDir || typeof agentDir !== "string") {
    return null;
  }
  const modelsJsonPath = path.join(agentDir, "models.json");
  if (!fs.existsSync(modelsJsonPath)) {
    return null;
  }
  return modelsJsonPath;
}

function createYinshiCodingTools(cwd, gitAuth) {
  return [
    createReadTool(cwd),
    createGitAwareBashTool(cwd, gitAuth),
    createEditTool(cwd),
    createWriteTool(cwd),
  ];
}

// Pi's Theme is a color/styling helper for terminal output. In a web chat
// we have no ANSI support, so every helper returns the text unchanged. The
// shape matches interactive/theme/theme.d.ts so extensions calling
// ctx.ui.theme.fg(...) or ctx.ui.theme.strikethrough(...) don't throw.
function createPassthroughTheme() {
  const passthrough = (_color, text) => (typeof text === "string" ? text : String(text ?? ""));
  const onlyText = (text) => (typeof text === "string" ? text : String(text ?? ""));
  return {
    fg: passthrough,
    bg: passthrough,
    bold: onlyText,
    italic: onlyText,
    underline: onlyText,
    inverse: onlyText,
    strikethrough: onlyText,
    getFgAnsi() {
      return "";
    },
    getBgAnsi() {
      return "";
    },
    getColorMode() {
      return "none";
    },
    getThinkingBorderColor() {
      return onlyText;
    },
    getBashModeBorderColor() {
      return onlyText;
    },
    name: "web",
    path: undefined,
  };
}

// Extensions (rtk-metrics, plan-mode, etc.) drive their output through the
// same ExtensionUIContext that pi's interactive TUI implements. Without a
// bound context every method throws or no-ops and the command output never
// reaches the user. This adapter fills in the full surface: notify() is
// forwarded as chat text, dialog methods explain the limitation, and
// text-styling/theme helpers are passthroughs so calls like
// ctx.ui.theme.fg("accent", "...") don't throw inside a handler.
function createWebUIContext(sessionId, socket, model) {
  function emitAssistantText(message) {
    const text = typeof message === "string" ? message : String(message ?? "");
    sendToSocket(socket, {
      id: sessionId,
      type: "message",
      data: {
        type: "assistant",
        message: { content: [{ type: "text", text }] },
      },
    });
  }

  function emitWithLevel(message, level) {
    const prefix = level === "error" ? "Error: " : level === "warning" ? "Warning: " : "";
    emitAssistantText(prefix + (typeof message === "string" ? message : String(message ?? "")));
  }

  const theme = createPassthroughTheme();

  return {
    // ── notifications ────────────────────────────────────────────────
    notify(message, level = "info") {
      console.log(`[sidecar][ui.notify] level=${level}`);
      emitWithLevel(message, level);
    },
    // ── status/widget/title (TUI-only surfaces) ──────────────────────
    // Accept the calls so plan-mode and friends don't throw; the web UI
    // doesn't render these yet, so they're ignored rather than displayed.
    setStatus() {},
    setWorkingMessage() {},
    setWidget() {},
    setHeader() {},
    setFooter() {},
    setTitle() {},
    // ── interactive dialogs ──────────────────────────────────────────
    // None of these have a web equivalent yet. Emit a brief explanation
    // so the user understands why a command that would prompt in local
    // pi just terminates here, and return sensible defaults.
    async select() {
      emitAssistantText(
        "Interactive selection is not yet supported in the web UI; cancelling the prompt.",
      );
      return undefined;
    },
    async confirm() {
      emitAssistantText(
        "Interactive confirmation is not yet supported in the web UI; defaulting to no.",
      );
      return false;
    },
    async input() {
      emitAssistantText(
        "Interactive text input is not yet supported in the web UI; cancelling the prompt.",
      );
      return undefined;
    },
    async editor() {
      emitAssistantText(
        "The multi-line editor is not yet supported in the web UI; cancelling the prompt.",
      );
      return undefined;
    },
    async custom() {
      emitAssistantText(
        "Custom overlays are not yet supported in the web UI; cancelling the prompt.",
      );
      return undefined;
    },
    // ── editor shims (no-op since there's no pi TUI input line) ──────
    pasteToEditor() {},
    setEditorText() {},
    getEditorText() {
      return "";
    },
    setEditorComponent() {},
    onTerminalInput() {
      return () => {};
    },
    // ── theme surface ────────────────────────────────────────────────
    theme,
    getAllThemes() {
      return [{ name: theme.name, path: undefined }];
    },
    getTheme() {
      return theme;
    },
    setTheme() {
      return { success: false, error: "Theme switching is not supported in the web UI" };
    },
    // ── tool output expansion (TUI setting, N/A for chat) ────────────
    getToolsExpanded() {
      return true;
    },
    setToolsExpanded() {},
    // Defensive metadata in case extensions read non-standard properties.
    modelName: model?.name,
  };
}

function normalizeApiKeyWithConfigSecret(secret) {
  if (typeof secret === "string") {
    const normalizedSecret = secret.trim();
    if (!normalizedSecret) {
      throw new Error("API key + config auth requires a non-empty apiKey");
    }
    return { apiKey: normalizedSecret };
  }
  if (!secret || typeof secret !== "object" || Array.isArray(secret)) {
    throw new Error("API key + config auth requires an object secret");
  }
  const normalizedSecret = {};
  for (const [key, value] of Object.entries(secret)) {
    if (typeof value !== "string") {
      throw new Error(`API key + config secret field ${key} must be a string`);
    }
    const trimmedValue = value.trim();
    if (!trimmedValue) {
      throw new Error(`API key + config secret field ${key} must not be empty`);
    }
    normalizedSecret[key] = trimmedValue;
  }
  if (typeof normalizedSecret.apiKey !== "string" || !normalizedSecret.apiKey) {
    throw new Error("API key + config auth requires an apiKey field");
  }
  return normalizedSecret;
}

async function writeCredential(credentials, provider, credential) {
  await credentials.modify(
    provider,
    async () => credential,
    { signal: AbortSignal.timeout(5_000) },
  );
}

async function createCredentialStore(providerAuth) {
  const credentials = new InMemoryCredentialStore();
  if (!providerAuth || typeof providerAuth !== "object") {
    return credentials;
  }
  if (typeof providerAuth.provider !== "string" || !providerAuth.provider) {
    throw new Error("providerAuth.provider must be a non-empty string");
  }
  if (typeof providerAuth.authStrategy !== "string" || !providerAuth.authStrategy) {
    throw new Error("providerAuth.authStrategy must be a non-empty string");
  }
  if (providerAuth.authStrategy === "api_key") {
    if (typeof providerAuth.secret !== "string" || !providerAuth.secret) {
      throw new Error("API key auth requires a non-empty secret");
    }
    await writeCredential(credentials, providerAuth.provider, {
      type: "api_key",
      key: providerAuth.secret,
    });
    return credentials;
  }
  if (providerAuth.authStrategy === "api_key_with_config") {
    const normalizedSecret = normalizeApiKeyWithConfigSecret(providerAuth.secret);
    const { apiKey, ...env } = normalizedSecret;
    await writeCredential(credentials, providerAuth.provider, {
      type: "api_key",
      key: apiKey,
      env,
    });
    return credentials;
  }
  if (providerAuth.authStrategy === "oauth") {
    if (!providerAuth.secret || typeof providerAuth.secret !== "object" || Array.isArray(providerAuth.secret)) {
      throw new Error("OAuth auth requires an object secret");
    }
    await writeCredential(credentials, providerAuth.provider, {
      type: "oauth",
      ...providerAuth.secret,
    });
    return credentials;
  }
  throw new Error(`Unsupported auth strategy: ${providerAuth.authStrategy}`);
}

async function createModelRegistry(providerAuth, agentDir) {
  const credentials = await createCredentialStore(providerAuth);
  const modelsPath = buildModelsJsonPath(agentDir);
  // Pass null when there is no imported agentDir so the SDK does not fall back
  // to the host machine's ~/.pi/agent/models.json or auth.json.
  const modelRuntime = await ModelRuntime.create({
    credentials,
    modelsPath,
    allowModelNetwork: false,
    refreshOnCreate: false,
  });
  const registry = new ModelRegistry(modelRuntime);
  return { credentials, modelRuntime, registry };
}

function getThinkingLevels(model) {
  if (!model.reasoning) {
    return [OFF_THINKING_LEVEL];
  }
  const supportedLevels = new Set(getSupportedThinkingLevels(model));
  return supportedLevels.has("xhigh") ? XHIGH_THINKING_LEVELS : STANDARD_THINKING_LEVELS;
}

function toCatalogModel(model) {
  return {
    ref: `${model.provider}/${model.id}`,
    provider: model.provider,
    id: model.id,
    label: model.name,
    api: model.api,
    reasoning: Boolean(model.reasoning),
    thinking_levels: getThinkingLevels(model),
    inputs: [...model.input],
    context_window: model.contextWindow,
    max_tokens: model.maxTokens,
  };
}

function toCatalogProvider(providerId, models) {
  return {
    id: providerId,
    model_count: models.length,
  };
}

async function getCatalog(agentDir) {
  const { registry } = await createModelRegistry(null, agentDir);
  const models = registry.getAll().map(toCatalogModel);
  const providerIds = new Set(models.map((model) => model.provider));
  const providers = [...providerIds]
    .sort()
    .map((providerId) => {
      const providerModels = models.filter((model) => model.provider === providerId);
      return toCatalogProvider(providerId, providerModels);
    });
  return {
    default_model: DEFAULT_MODEL_REF,
    providers,
    models,
  };
}

function findPackageJson(packageName) {
  if (typeof packageName !== "string" || !packageName) {
    throw new Error("packageName must be a non-empty string");
  }

  let currentPath = __sidecarDir;
  while (true) {
    const packageJsonPath = path.join(currentPath, "node_modules", packageName, "package.json");
    if (fs.existsSync(packageJsonPath)) {
      const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf-8"));
      if (packageJson.name === packageName) {
        return packageJson;
      }
    }
    const parentPath = path.dirname(currentPath);
    if (parentPath === currentPath) {
      break;
    }
    currentPath = parentPath;
  }
  throw new Error(`Unable to locate package.json for ${packageName}`);
}

function getRuntimeVersion() {
  const packageJson = findPackageJson(PI_PACKAGE_NAME);
  if (typeof packageJson.version !== "string" || !packageJson.version) {
    throw new Error(`${PI_PACKAGE_NAME} package.json is missing a version`);
  }
  return {
    package_name: PI_PACKAGE_NAME,
    installed_version: packageJson.version,
    node_version: process.version,
  };
}

// Pi skill/prompt/extension names come from user-uploaded zips. Constrain them
// to a conservative character class so they can't break palette rendering or
// leak control chars into any downstream string interpolation. 64 chars matches
// pi's own skill-name validation.
const NAME_PATTERN = /^[a-zA-Z0-9_:.\-]{1,64}$/;
const DESCRIPTION_LENGTH_MAX = 240;
// Hard cap per category so a pathological zip can't balloon the JSON line or
// the browser palette. Real configs are usually dozens; 500 is generous.
const COMMANDS_PER_CATEGORY_MAX = 500;

function safeName(rawName) {
  if (typeof rawName !== "string") {
    return null;
  }
  return NAME_PATTERN.test(rawName) ? rawName : null;
}

function safeDescription(rawDescription) {
  if (typeof rawDescription !== "string") {
    return "";
  }
  return rawDescription.length > DESCRIPTION_LENGTH_MAX
    ? rawDescription.slice(0, DESCRIPTION_LENGTH_MAX)
    : rawDescription;
}

function skillToCommand(skill) {
  const name = safeName(skill?.name);
  if (name === null) {
    return null;
  }
  return {
    kind: "skill",
    name,
    description: safeDescription(skill?.description),
    command_name: `skill:${name}`,
  };
}

function promptToCommand(prompt) {
  const name = safeName(prompt?.name);
  if (name === null) {
    return null;
  }
  return {
    kind: "prompt",
    name,
    description: safeDescription(prompt?.description),
    command_name: name,
  };
}

function extensionToCommands(extension) {
  // Pi exposes extension-registered slash commands via the Extension.commands map
  // populated during loader.reload(); keys are the invocation names.
  if (!extension || !(extension.commands instanceof Map)) {
    return [];
  }
  const commands = [];
  for (const [rawCommandName, registered] of extension.commands.entries()) {
    const name = safeName(rawCommandName);
    if (name === null) {
      continue;
    }
    commands.push({
      kind: "extension",
      name,
      description: safeDescription(registered?.description),
      command_name: name,
    });
  }
  return commands;
}

// Cache the list_resources payload per agentDir keyed by the most-recent
// mtime across the agent tree. Without this, every session mount re-evaluates
// every extension module via jiti (moduleCache:false in the pi SDK), which
// DoS-exposes the shared Node event loop. The cache keeps a small bound with
// deterministic least-recently-used eviction, so long-lived processes that see
// many unique agent directories evict old payloads instead of growing memory
// forever. Eviction only drops map entries; an in-flight request keeps its own
// reference, so active requests never lose data they still need.
export const LIST_RESOURCES_CACHE_MAX = 16;

// Minimal Map-recency bounded cache shared with the resource-list path.
export function createBoundedCache(maxEntries) {
  if (!Number.isSafeInteger(maxEntries) || maxEntries < 1) {
    throw new TypeError("maxEntries must be a positive safe integer");
  }
  const entries = new Map();
  return {
    get(key) {
      if (!entries.has(key)) {
        return undefined;
      }
      const entry = entries.get(key);
      entries.delete(key);
      entries.set(key, entry);
      return entry;
    },
    set(key, entry) {
      if (entries.has(key)) {
        entries.delete(key);
      }
      entries.set(key, entry);
      while (entries.size > maxEntries) {
        const oldestKey = entries.keys().next().value;
        entries.delete(oldestKey);
      }
    },
    size() {
      return entries.size;
    },
  };
}

// The production instance stays private to this module so callers and tests
// cannot mutate shared cache state. Tests instantiate their own bounded cache.
const _resourceListCache = createBoundedCache(LIST_RESOURCES_CACHE_MAX);

async function _collectAgentDirMtime(agentDir) {
  // Find the max mtime across the agent tree. A skill file edit, a new
  // extension dropped in, or a category-toggle rename all change this.
  let maxMtimeMs = 0;
  const pending = [agentDir];
  while (pending.length > 0) {
    const current = pending.pop();
    let stats;
    try {
      stats = await fs.promises.stat(current);
    } catch {
      continue;
    }
    if (stats.mtimeMs > maxMtimeMs) {
      maxMtimeMs = stats.mtimeMs;
    }
    if (!stats.isDirectory()) {
      continue;
    }
    let entries;
    try {
      entries = await fs.promises.readdir(current);
    } catch {
      continue;
    }
    for (const entry of entries) {
      pending.push(path.join(current, entry));
    }
  }
  return maxMtimeMs;
}

function _capCommands(commands) {
  return commands.length > COMMANDS_PER_CATEGORY_MAX
    ? commands.slice(0, COMMANDS_PER_CATEGORY_MAX)
    : commands;
}

export async function listResources(agentDir) {
  // Without an imported agentDir we intentionally return nothing: the SDK would
  // otherwise fall back to the host's ~/.pi/agent and leak host-local resources.
  if (!agentDir || typeof agentDir !== "string") {
    return { commands: [] };
  }

  const mtimeMs = await _collectAgentDirMtime(agentDir);
  const cached = _resourceListCache.get(agentDir);
  if (cached && cached.mtimeMs === mtimeMs) {
    return cached.payload;
  }

  const loader = new DefaultResourceLoader({
    // Use a neutral cwd so project-local .pi/ directories under the sidecar's
    // working directory don't bleed into this user's resource listing.
    cwd: os.tmpdir(),
    agentDir,
  });
  await loader.reload();

  const skillsResult = loader.getSkills();
  const promptsResult = loader.getPrompts();
  const extensionsResult = loader.getExtensions();

  const skills = Array.isArray(skillsResult?.skills) ? skillsResult.skills : [];
  const prompts = Array.isArray(promptsResult?.prompts) ? promptsResult.prompts : [];
  const extensions = Array.isArray(extensionsResult?.extensions) ? extensionsResult.extensions : [];

  const skillCommands = _capCommands(skills.map(skillToCommand).filter((c) => c !== null));
  const promptCommands = _capCommands(prompts.map(promptToCommand).filter((c) => c !== null));
  const extensionCommands = _capCommands(extensions.flatMap(extensionToCommands));

  const payload = {
    commands: [...skillCommands, ...promptCommands, ...extensionCommands],
  };
  _resourceListCache.set(agentDir, { mtimeMs, payload });
  return payload;
}

function _normalizeManualInputPrompt(promptMessage) {
  if (typeof promptMessage === "string") {
    const normalizedPromptMessage = promptMessage.trim();
    if (normalizedPromptMessage) {
      return normalizedPromptMessage;
    }
  }
  return "Paste the final redirect URL or authorization code here.";
}

function _buildHostedCallbackInstructions(baseInstructions) {
  const instructionParts = [];
  if (typeof baseInstructions === "string") {
    const normalizedBaseInstructions = baseInstructions.trim();
    if (normalizedBaseInstructions) {
      instructionParts.push(normalizedBaseInstructions);
    }
  }
  instructionParts.push(
    "If the browser lands on a localhost URL and shows an error, copy the full URL from the address bar and paste it back into Yinshi.",
  );
  return instructionParts.join(" ");
}

function _waitForOAuthManualInput(flow, promptMessage) {
  if (!flow || typeof flow !== "object") {
    throw new Error("OAuth flow is required");
  }
  if (flow.manualInputSubmitted) {
    if (typeof flow.manualInputValue !== "string" || !flow.manualInputValue) {
      throw new Error("Submitted OAuth manual input is missing");
    }
    return Promise.resolve(flow.manualInputValue);
  }
  flow.manualInputRequired = true;
  flow.manualInputPrompt = _normalizeManualInputPrompt(promptMessage);
  if (flow.manualInputPromise) {
    return flow.manualInputPromise;
  }
  flow.manualInputPromise = new Promise((resolve, reject) => {
    flow.manualInputResolve = resolve;
    flow.manualInputReject = reject;
  });
  return flow.manualInputPromise;
}

function _submitOAuthManualInput(flow, authorizationInput) {
  if (!flow || typeof flow !== "object") {
    throw new Error("OAuth flow is required");
  }
  if (typeof authorizationInput !== "string") {
    throw new Error("authorizationInput must be a string");
  }
  const normalizedAuthorizationInput = authorizationInput.trim();
  if (!normalizedAuthorizationInput) {
    throw new Error("authorizationInput must not be empty");
  }
  if (flow.manualInputSubmitted) {
    throw new Error("OAuth manual input was already submitted");
  }
  flow.manualInputRequired = true;
  flow.manualInputSubmitted = true;
  flow.manualInputValue = normalizedAuthorizationInput;
  flow.progress.push("Received manual OAuth callback input.");
  if (flow.manualInputResolve) {
    flow.manualInputResolve(normalizedAuthorizationInput);
    flow.manualInputResolve = null;
    flow.manualInputReject = null;
  } else {
    flow.manualInputPromise = Promise.resolve(normalizedAuthorizationInput);
  }
}

function resolveModelFromRegistry(registry, modelKey, providerConfig) {
  const normalizedLookup = normalizeModelLookup(modelKey || DEFAULT_MODEL_REF);
  const models = registry.getAll();

  if (normalizedLookup.includes("/")) {
    const slashIndex = normalizedLookup.indexOf("/");
    const provider = normalizedLookup.slice(0, slashIndex);
    const modelId = normalizedLookup.slice(slashIndex + 1);
    const resolved = registry.find(provider, modelId);
    if (!resolved) {
      throw new Error(`Unknown model: ${modelKey}`);
    }
    return applyProviderConfig(resolved, providerConfig);
  }

  const directMatches = models.filter(
    (model) => model.id.toLowerCase() === normalizedLookup.toLowerCase(),
  );
  if (directMatches.length === 1) {
    return applyProviderConfig(directMatches[0], providerConfig);
  }

  const labelMatches = models.filter(
    (model) => model.name.toLowerCase() === normalizedLookup.toLowerCase(),
  );
  if (labelMatches.length === 1) {
    return applyProviderConfig(labelMatches[0], providerConfig);
  }

  throw new Error(`Unknown model: ${modelKey}`);
}

async function resolveModel(modelKey, providerAuth, agentDir, providerConfig) {
  const { registry } = await createModelRegistry(providerAuth, agentDir);
  return resolveModelFromRegistry(registry, modelKey, providerConfig);
}

function applyProviderConfig(model, providerConfig) {
  if (!providerConfig || typeof providerConfig !== "object") {
    return model;
  }
  if (model.provider !== "azure-openai-responses") {
    return model;
  }

  const configuredModel = { ...model };
  if (typeof providerConfig.baseUrl === "string" && providerConfig.baseUrl.trim()) {
    configuredModel.baseUrl = providerConfig.baseUrl.trim();
  }
  if (typeof providerConfig.azureDeploymentName === "string" && providerConfig.azureDeploymentName.trim()) {
    configuredModel.azureDeploymentName = providerConfig.azureDeploymentName.trim();
  }
  return configuredModel;
}

async function resolveProviderRuntimeAuth(provider, modelRef, providerAuth, agentDir, providerConfig) {
  if (!providerAuth || typeof providerAuth !== "object") {
    return {
      provider,
      auth: null,
      model_ref: modelRef,
      runtime_api_key: null,
      model_config: providerConfig || null,
    };
  }

  const { credentials, modelRuntime, registry } = await createModelRegistry(providerAuth, agentDir);
  const runtimeAuth = await modelRuntime.getAuth(provider);
  const runtimeApiKey = runtimeAuth?.auth.apiKey;
  const credential = await credentials.read(provider);
  const resolvedModel = resolveModelFromRegistry(registry, modelRef, providerConfig);
  const modelConfig = {};
  if (resolvedModel.provider === "github-copilot" && typeof resolvedModel.baseUrl === "string") {
    modelConfig.baseUrl = resolvedModel.baseUrl;
  }
  if (resolvedModel.provider === "azure-openai-responses") {
    if (typeof resolvedModel.baseUrl === "string" && resolvedModel.baseUrl) {
      modelConfig.baseUrl = resolvedModel.baseUrl;
    }
    if (typeof resolvedModel.azureDeploymentName === "string" && resolvedModel.azureDeploymentName) {
      modelConfig.azureDeploymentName = resolvedModel.azureDeploymentName;
    }
  }
  let returnedAuth = providerAuth.secret ?? null;
  if (providerAuth.authStrategy === "oauth") {
    returnedAuth = credential || null;
  }
  if (providerAuth.authStrategy === "api_key_with_config") {
    returnedAuth = normalizeApiKeyWithConfigSecret(providerAuth.secret);
  }
  return {
    provider,
    auth: returnedAuth,
    model_ref: `${resolvedModel.provider}/${resolvedModel.id}`,
    runtime_api_key: runtimeApiKey || null,
    model_config: Object.keys(modelConfig).length > 0 ? modelConfig : null,
  };
}

export class YinshiSidecar {
  constructor(options = {}) {
    if (!options || typeof options !== "object" || Array.isArray(options)) {
      throw new TypeError("Sidecar options must be an object");
    }
    const modelRegistryFactory = options.modelRegistryFactory ?? createModelRegistry;
    if (typeof modelRegistryFactory !== "function") {
      throw new TypeError("modelRegistryFactory must be a function");
    }
    const oauthLoginMode =
      options.oauthLoginMode ?? process.env.YINSHI_SIDECAR_OAUTH_MODE ?? "";
    if (typeof oauthLoginMode !== "string") {
      throw new TypeError("oauthLoginMode must be a string");
    }
    this.modelRegistryFactory = modelRegistryFactory;
    this.oauthLoginMode = oauthLoginMode.trim();
    this.activeSessions = new Map();
    this.activePromptSessionsBySocket = new Map();
    this.pendingPiSessionCreations = new Map();
    this.activeOAuthFlows = new Map();
    this.activeTerminals = new Map();
    this.socketPath = process.env.SIDECAR_SOCKET_PATH || "/tmp/yinshi-sidecar.sock";
    this.server = net.createServer((socket) => this.handleConnection(socket));
    this.healthCheckInterval = null;
  }

  initialize() {
    if (process.env.SIDECAR_LOAD_DOTENV === "1") {
      this._loadDotEnv();
    }
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
      if (!trimmed || trimmed.startsWith("#")) {
        continue;
      }
      const eqIndex = trimmed.indexOf("=");
      if (eqIndex === -1) {
        continue;
      }
      const key = trimmed.slice(0, eqIndex).trim();
      const value = trimmed.slice(eqIndex + 1).trim();
      if (!process.env[key]) {
        process.env[key] = value;
        console.log("[sidecar] Loaded one environment entry");
      }
    }
  }

  createTerminalEntry(id, options) {
    const cwd = normalizeTerminalCwd(options.cwd);
    const cols = normalizePositiveInteger(options.cols, 100, 20, 300);
    const rows = normalizePositiveInteger(options.rows, 30, 5, 120);
    const scrollbackLines = normalizePositiveInteger(options.scrollbackLines, 1000, 100, 5000);
    const shell = process.env.SHELL || "/bin/bash";
    const terminalEnvironment = buildTerminalEnvironment(cwd, shell);
    const terminal = pty.spawn(shell, ["-l"], {
      name: "xterm-256color",
      cols,
      rows,
      cwd,
      env: terminalEnvironment,
    });
    const entry = {
      id,
      cwd,
      terminal,
      sockets: new Set(),
      scrollback: "",
      scrollbackLimitBytes: scrollbackLines * 200,
      suppressExitEvent: false,
    };
    terminal.onData((data) => {
      appendTerminalScrollback(entry, data);
      for (const subscriber of entry.sockets) {
        sendToSocket(subscriber, { id, type: "terminal_data", data });
      }
    });
    terminal.onExit(({ exitCode, signal }) => {
      if (!entry.suppressExitEvent) {
        for (const subscriber of entry.sockets) {
          sendToSocket(subscriber, {
            id,
            type: "terminal_exit",
            exit_code: exitCode,
            signal,
          });
        }
      }
      if (this.activeTerminals.get(id) === entry) {
        this.activeTerminals.delete(id);
      }
    });
    this.activeTerminals.set(id, entry);
    return entry;
  }

  terminalEntry(id, options, restart = false) {
    if (restart) {
      const previous = this.activeTerminals.get(id);
      if (previous) {
        // Detach subscribers from the dying PTY first so its late output and
        // suppressed exit event can never reach clients moving to the restart.
        previous.sockets.clear();
      }
      this.killTerminal(id, { suppressExitEvent: true });
    }
    const existing = this.activeTerminals.get(id);
    if (existing) {
      return existing;
    }
    return this.createTerminalEntry(id, options);
  }

  attachTerminal(id, socket, options, restart = false) {
    const previousEntry = restart ? this.activeTerminals.get(id) : null;
    const otherSockets = previousEntry
      ? [...previousEntry.sockets].filter(
          (candidate) => candidate !== socket && !candidate.destroyed,
        )
      : [];
    const entry = this.terminalEntry(id, options, restart);
    for (const otherSocket of otherSockets) {
      entry.sockets.add(otherSocket);
      sendToSocket(otherSocket, {
        id,
        type: "terminal_ready",
        cwd: entry.cwd,
        pid: entry.terminal.pid,
        replay: entry.scrollback,
        restarted: true,
      });
    }
    entry.sockets.add(socket);
    sendToSocket(socket, {
      id,
      type: "terminal_ready",
      cwd: entry.cwd,
      pid: entry.terminal.pid,
      replay: entry.scrollback,
    });
  }

  detachTerminalSocket(socket) {
    for (const entry of this.activeTerminals.values()) {
      entry.sockets.delete(socket);
    }
  }

  writeTerminal(id, data) {
    const entry = this.activeTerminals.get(id);
    if (!entry) {
      throw new Error("Terminal is not running");
    }
    if (typeof data !== "string") {
      throw new Error("terminal input data must be a string");
    }
    entry.terminal.write(data);
  }

  resizeTerminal(id, cols, rows) {
    const entry = this.activeTerminals.get(id);
    if (!entry) {
      throw new Error("Terminal is not running");
    }
    entry.terminal.resize(
      normalizePositiveInteger(cols, 100, 20, 300),
      normalizePositiveInteger(rows, 30, 5, 120),
    );
  }

  killTerminal(id, { suppressExitEvent = false } = {}) {
    const entry = this.activeTerminals.get(id);
    if (!entry) {
      return;
    }
    entry.suppressExitEvent = Boolean(suppressExitEvent);
    entry.terminal.kill();
    this.activeTerminals.delete(id);
  }

  async start() {
    this.cleanup();

    return new Promise((resolve, reject) => {
      this.server.listen(this.socketPath, () => {
        try {
          fs.chmodSync(this.socketPath, 0o600);
        } catch (error) {
          this.server.close();
          reject(error);
          return;
        }
        console.log(`SOCKET_PATH=${this.socketPath}`);
        this.healthCheckInterval = setInterval(() => {
          console.log(
            `[sidecar] Health: ${this.activeSessions.size} session(s), ${this.activeOAuthFlows.size} auth flow(s), ${this.activeTerminals.size} terminal(s)`,
          );
        }, HEALTH_CHECK_INTERVAL);
        resolve();
      });
      this.server.on("error", (err) => {
        console.error("[sidecar] Server error");
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
        if (!trimmed) {
          continue;
        }
        this.handleData(trimmed, socket);
      }
    });
    socket.on("error", () => {
      console.error("[sidecar] Socket error");
      this._cancelPromptsForSocket(socket);
    });
    socket.on("close", () => {
      this._cancelPromptsForSocket(socket);
      this.detachTerminalSocket(socket);
      console.log("[sidecar] Connection closed");
    });
  }

  _trackPromptSession(socket, sessionId) {
    let promptsBySession = this.activePromptSessionsBySocket.get(socket);
    if (!promptsBySession) {
      promptsBySession = new Map();
      this.activePromptSessionsBySocket.set(socket, promptsBySession);
    }
    let promptStates = promptsBySession.get(sessionId);
    if (!promptStates) {
      promptStates = new Set();
      promptsBySession.set(sessionId, promptStates);
    }
    const promptState = {
      sessionId,
      cancelled: false,
      executionStarted: false,
      cancelRequested: false,
    };
    promptStates.add(promptState);
    return promptState;
  }

  _untrackPromptSession(socket, promptState) {
    const promptsBySession = this.activePromptSessionsBySocket.get(socket);
    const promptStates = promptsBySession?.get(promptState.sessionId);
    if (!promptStates) {
      return;
    }
    promptStates.delete(promptState);
    if (promptStates.size === 0) {
      promptsBySession.delete(promptState.sessionId);
    }
    if (promptsBySession.size === 0) {
      this.activePromptSessionsBySocket.delete(socket);
    }
  }

  _cancelPromptsForSocket(socket) {
    const promptsBySession = this.activePromptSessionsBySocket.get(socket);
    if (!promptsBySession) {
      return;
    }
    for (const [sessionId, promptStates] of promptsBySession) {
      let cancellationNeeded = false;
      for (const promptState of promptStates) {
        promptState.cancelled = true;
        if (promptState.executionStarted && !promptState.cancelRequested) {
          promptState.cancelRequested = true;
          cancellationNeeded = true;
        }
      }
      // The transport is gone: pending backend calls can never be answered.
      // Dispose the channel immediately so tools waiting on a response
      // settle now instead of deadlocking until the prompt unwinds.
      const orchestration = this.activeSessions.get(sessionId)?.orchestration;
      if (orchestration?.rpc && orchestration.socket === socket) {
        orchestration.rpc.dispose();
      }
      if (!cancellationNeeded || this.activeSessions.get(sessionId)?.cancelRequested) {
        continue;
      }
      void this.cancelSession(sessionId).catch(() => {
        console.error("[sidecar] Disconnected prompt cancellation failed");
      });
    }
  }

  _sessionHasActivePrompt(sessionId) {
    for (const promptsBySession of this.activePromptSessionsBySocket.values()) {
      if (promptsBySession.has(sessionId)) {
        return true;
      }
    }
    return false;
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

  _pruneIdlePiSessions(currentTimeMs) {
    if (!Number.isSafeInteger(currentTimeMs) || currentTimeMs < 0) {
      throw new TypeError("Pi session clock must be a non-negative safe integer");
    }
    for (const [sessionId, entry] of this.activeSessions) {
      if (this._sessionHasActivePrompt(sessionId)) {
        continue;
      }
      const lastActivityMs = entry?.lastActivityMs;
      if (!Number.isSafeInteger(lastActivityMs)) {
        // A session from an older code path has no stamp yet. Adopt it rather
        // than destroy work that is still in progress.
        entry.lastActivityMs = currentTimeMs;
        continue;
      }
      if (currentTimeMs - lastActivityMs < PI_SESSION_IDLE_TTL_MS) {
        continue;
      }
      this.activeSessions.delete(sessionId);
      this._disposePiSessionEntry(entry);
    }
  }

  _evictLeastRecentPiSessions() {
    if (this.activeSessions.size <= PI_SESSION_COUNT_MAX) {
      return;
    }
    const byOldestFirst = [...this.activeSessions.entries()]
      .filter(([sessionId]) => !this._sessionHasActivePrompt(sessionId))
      .sort((left, right) => left[1].lastActivityMs - right[1].lastActivityMs);
    const excess = this.activeSessions.size - PI_SESSION_COUNT_MAX;
    for (const [sessionId, entry] of byOldestFirst.slice(0, excess)) {
      this.activeSessions.delete(sessionId);
      this._disposePiSessionEntry(entry);
    }
  }

  _touchPiSession(sessionId, currentTimeMs) {
    const entry = this.activeSessions.get(sessionId);
    if (entry) {
      entry.lastActivityMs = currentTimeMs;
    }
  }

  handleRequest(request, socket, currentTimeMs = Date.now()) {
    this._pruneIdlePiSessions(currentTimeMs);
    this._evictLeastRecentPiSessions();
    if (request && typeof request === "object") {
      this._touchPiSession(request.id, currentTimeMs);
    }
    if (!request || typeof request !== "object") {
      sendToSocket(socket, { id: "unknown", type: "error", error: "Invalid request format" });
      return;
    }
    const { type, id } = request;
    switch (type) {
      case "auth_resolve":
        void this.handleAuthResolve(id, socket, request);
        break;
      case "cancel":
        void this.handleCancelRequest(id, socket);
        break;
      case "catalog":
        void this.handleCatalog(id, socket, request.options || {});
        break;
      case "version":
        this.handleVersion(id, socket);
        break;
      case "list_resources":
        void this.handleListResources(id, socket, request.options || {});
        break;
      case "oauth_clear":
        this.clearOAuthFlow(id, socket, request.flowId);
        break;
      case "oauth_start":
        void this.startOAuthFlow(id, socket, request.provider);
        break;
      case "oauth_status":
        this.handleOAuthStatus(id, socket, request.flowId);
        break;
      case "oauth_submit":
        this.submitOAuthFlowInput(id, socket, request.flowId, request.authorizationInput);
        break;
      case "ping":
        sendToSocket(socket, { type: "pong" });
        break;
      case "orchestration_response": {
        // Route the backend answer to the pending tool promise of the
        // addressed session. Only the socket that started the query may
        // deliver responses. Unknown, late, or duplicate responses are
        // consumed silently so they can never corrupt a later query.
        const targetEntry = typeof id === "string" ? this.activeSessions.get(id) : null;
        const orchestration = targetEntry?.orchestration;
        if (
          orchestration?.rpc
          && orchestration.socket === socket
          && orchestration.rpc.handleFrame(request)
        ) {
          break;
        }
        console.log("[sidecar] Dropped unmatched orchestration response");
        break;
      }
      case "terminal_attach":
        this.handleTerminalAttach(id, socket, request.options || {});
        break;
      case "terminal_detach":
        this.detachTerminalSocket(socket);
        sendToSocket(socket, { id: id || "terminal", type: "terminal_detached" });
        break;
      case "terminal_input":
        this.handleTerminalInput(id, socket, request.data);
        break;
      case "terminal_kill":
        this.handleTerminalKill(id, socket);
        break;
      case "terminal_resize":
        this.handleTerminalResize(id, socket, request.cols, request.rows);
        break;
      case "terminal_restart":
        this.handleTerminalAttach(id, socket, request.options || {}, true);
        break;
      case "query":
        void this.processQuery(id, socket, request.prompt, request.options || {});
        break;
      case "session_release":
        sendToSocket(socket, {
          id: id || "unknown",
          type: "session_released",
          released: this.releasePiSession(id),
        });
        break;
      case "resolve":
        void this.handleResolve(id, socket, request.model, request.options || {});
        break;
      case "warmup":
        void this.warmupSession(id, socket, request.options || {});
        break;
      default:
        sendToSocket(socket, { id: id || "unknown", type: "error", error: `Unknown request type: ${type}` });
    }
  }

  handleTerminalAttach(id, socket, options, restart = false) {
    try {
      const terminalId = normalizeTerminalId(options.workspaceId || id);
      this.attachTerminal(terminalId, socket, options, restart);
    } catch (err) {
      sendTerminalError(socket, id, err, "Failed to attach terminal");
    }
  }

  handleTerminalInput(id, socket, data) {
    try {
      const terminalId = normalizeTerminalId(id);
      this.writeTerminal(terminalId, data);
    } catch (err) {
      sendTerminalError(socket, id, err, "Failed to write terminal input");
    }
  }

  handleTerminalResize(id, socket, cols, rows) {
    try {
      const terminalId = normalizeTerminalId(id);
      this.resizeTerminal(terminalId, cols, rows);
    } catch (err) {
      sendTerminalError(socket, id, err, "Failed to resize terminal");
    }
  }

  handleTerminalKill(id, socket) {
    try {
      const terminalId = normalizeTerminalId(id);
      this.killTerminal(terminalId);
      sendToSocket(socket, { id: terminalId, type: "terminal_killed" });
    } catch (err) {
      sendTerminalError(socket, id, err, "Failed to kill terminal");
    }
  }

  async handleCatalog(id, socket, options) {
    try {
      const catalog = await getCatalog(options.agentDir || null);
      sendToSocket(socket, {
        id: id || "catalog",
        type: "catalog",
        ...catalog,
      });
    } catch (err) {
      sendToSocket(socket, {
        id: id || "catalog",
        type: "error",
        error: err instanceof Error ? err.message : "Failed to build model catalog",
      });
    }
  }

  handleVersion(id, socket) {
    try {
      sendToSocket(socket, {
        id: id || "version",
        type: "version",
        ...getRuntimeVersion(),
      });
    } catch (err) {
      sendToSocket(socket, {
        id: id || "version",
        type: "error",
        error: err instanceof Error ? err.message : "Failed to read pi package version",
      });
    }
  }

  async handleListResources(id, socket, options) {
    try {
      const resources = await listResources(options.agentDir || null);
      sendToSocket(socket, {
        id: id || "list_resources",
        type: "resources",
        ...resources,
      });
    } catch (err) {
      sendToSocket(socket, {
        id: id || "list_resources",
        type: "error",
        error:
          err instanceof Error
            ? err.message
            : "Failed to list imported pi resources",
      });
    }
  }

  async handleResolve(id, socket, modelKey, options) {
    try {
      const resolved = await resolveModel(
        modelKey,
        options.providerAuth || null,
        options.agentDir || null,
        options.providerConfig || null,
      );
      sendToSocket(socket, {
        id,
        type: "resolved",
        provider: resolved.provider,
        model: `${resolved.provider}/${resolved.id}`,
      });
    } catch (err) {
      sendToSocket(socket, {
        id: id || "unknown",
        type: "error",
        error: err instanceof Error ? err.message : `Unknown model: ${modelKey}`,
      });
    }
  }

  async handleAuthResolve(id, socket, request) {
    try {
      if (typeof request.provider !== "string" || !request.provider) {
        throw new Error("Provider is required");
      }
      if (typeof request.model !== "string" || !request.model) {
        throw new Error("Model is required");
      }
      const resolved = await resolveProviderRuntimeAuth(
        request.provider,
        request.model,
        request.providerAuth || null,
        request.agentDir || null,
        request.providerConfig || null,
      );
      sendToSocket(socket, {
        id,
        type: "auth_resolved",
        ...resolved,
      });
    } catch (err) {
      sendToSocket(socket, {
        id: id || "auth-resolve",
        type: "error",
        error: err instanceof Error ? err.message : "Failed to resolve provider auth",
      });
    }
  }

  _pruneExpiredOAuthFlows(currentTimeMs = Date.now()) {
    if (!Number.isSafeInteger(currentTimeMs) || currentTimeMs < 0) {
      throw new TypeError("OAuth flow clock must be a non-negative safe integer");
    }
    for (const [flowId, flow] of this.activeOAuthFlows) {
      if (!Number.isSafeInteger(flow.createdAtMs)) {
        this.activeOAuthFlows.delete(flowId);
        continue;
      }
      if (currentTimeMs - flow.createdAtMs < OAUTH_FLOW_TTL_MS) {
        continue;
      }
      if (typeof flow.manualInputReject === "function") {
        flow.manualInputReject(new Error("OAuth flow expired"));
      }
      flow.manualInputResolve = null;
      flow.manualInputReject = null;
      this.activeOAuthFlows.delete(flowId);
    }
  }

  async startOAuthFlow(id, socket, providerId) {
    try {
      if (typeof providerId !== "string" || !providerId) {
        throw new Error("Provider is required");
      }
      const { modelRuntime } = await this.modelRegistryFactory(null, null);
      const provider = modelRuntime.getProvider(providerId);
      if (!provider?.auth.oauth) {
        throw new Error(`OAuth provider is not available: ${providerId}`);
      }
      this._pruneExpiredOAuthFlows();
      if (this.activeOAuthFlows.size >= OAUTH_FLOW_COUNT_MAX) {
        throw new Error("Too many active OAuth flows");
      }

      const flowId = randomUUID();
      const flow = {
        id: flowId,
        provider: providerId,
        createdAtMs: Date.now(),
        status: "starting",
        authorizationMode: null,
        authUrl: null,
        userCode: null,
        instructions: null,
        progress: [],
        credentials: null,
        error: null,
        manualInputRequired: Boolean(provider.usesCallbackServer),
        manualInputPrompt: provider.usesCallbackServer
          ? "Paste the final redirect URL or authorization code here."
          : null,
        manualInputSubmitted: false,
        manualInputValue: null,
        manualInputPromise: null,
        manualInputResolve: null,
        manualInputReject: null,
      };
      this.activeOAuthFlows.set(flowId, flow);

      const loginPromise = modelRuntime.login(providerId, "oauth", {
        prompt: async (prompt) => {
          if (prompt.type === "select" && prompt.options.length > 0) {
            const preferredOption = prompt.options.find(
              (option) => option.id === this.oauthLoginMode,
            );
            const selectedOption = preferredOption ?? prompt.options[0];
            flow.authorizationMode =
              selectedOption.id === "device_code" ? "device_code" : "browser";
            if (flow.authorizationMode === "device_code") {
              flow.manualInputRequired = false;
              flow.manualInputPrompt = null;
            }
            return selectedOption.id;
          }
          return _waitForOAuthManualInput(flow, prompt.message);
        },
        notify: (event) => {
          if (event.type === "auth_url") {
            if (flow.authorizationMode !== "device_code") {
              flow.authorizationMode = "browser";
              flow.userCode = null;
            }
            flow.authUrl = event.url;
            flow.instructions = event.instructions || null;
            flow.status = "pending";
            return;
          }
          if (event.type === "device_code") {
            if (
              typeof event.verificationUri !== "string" ||
              !event.verificationUri ||
              typeof event.userCode !== "string" ||
              !event.userCode
            ) {
              throw new Error("OAuth device authorization response is invalid");
            }
            flow.authorizationMode = "device_code";
            flow.authUrl = event.verificationUri;
            flow.userCode = event.userCode;
            flow.instructions =
              "Open the verification page and enter the displayed code.";
            flow.manualInputRequired = false;
            flow.manualInputPrompt = null;
            flow.status = "pending";
            return;
          }
          if (event.type === "progress" || event.type === "info") {
            flow.progress.push(event.message);
          }
        },
      });

      loginPromise
        .then((credentials) => {
          flow.credentials = credentials;
          if (flow.status === "starting") {
            flow.status = "pending";
          }
          flow.status = "complete";
        })
        .catch((err) => {
          flow.error = err instanceof Error ? err.message : String(err);
          flow.status = "error";
        });

      const startDeadline = Date.now() + 5_000;
      while (!flow.authUrl && flow.status !== "error" && Date.now() < startDeadline) {
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      if (!flow.authUrl && flow.status !== "error") {
        throw new Error("OAuth flow did not expose an authorization URL");
      }

      sendToSocket(socket, {
        id,
        type: "oauth_started",
        flow_id: flowId,
        provider: providerId,
        auth_url: flow.authUrl,
        authorization_mode: flow.authorizationMode,
        user_code: flow.userCode,
        instructions: flow.instructions,
        manual_input_required: flow.manualInputRequired,
        manual_input_prompt: flow.manualInputPrompt,
        manual_input_submitted: flow.manualInputSubmitted,
      });
    } catch (err) {
      sendToSocket(socket, {
        id: id || "oauth-start",
        type: "error",
        error: err instanceof Error ? err.message : "Failed to start OAuth flow",
      });
    }
  }

  handleOAuthStatus(id, socket, flowId) {
    this._pruneExpiredOAuthFlows();
    if (typeof flowId !== "string" || !flowId) {
      sendToSocket(socket, { id: id || "oauth-status", type: "error", error: "flowId is required" });
      return;
    }
    const flow = this.activeOAuthFlows.get(flowId);
    if (!flow) {
      sendToSocket(socket, { id: id || "oauth-status", type: "error", error: "OAuth flow not found" });
      return;
    }
    sendToSocket(socket, {
      id,
      type: "oauth_status",
      flow_id: flow.id,
      provider: flow.provider,
      status: flow.status,
      auth_url: flow.authUrl,
      authorization_mode: flow.authorizationMode,
      user_code: flow.userCode,
      instructions: flow.instructions,
      progress: flow.progress,
      credentials: flow.status === "complete" ? flow.credentials : null,
      error: flow.error,
      manual_input_required: flow.manualInputRequired,
      manual_input_prompt: flow.manualInputPrompt,
      manual_input_submitted: flow.manualInputSubmitted,
    });
  }

  submitOAuthFlowInput(id, socket, flowId, authorizationInput) {
    this._pruneExpiredOAuthFlows();
    if (typeof flowId !== "string" || !flowId) {
      sendToSocket(socket, { id: id || "oauth-submit", type: "error", error: "flowId is required" });
      return;
    }
    const flow = this.activeOAuthFlows.get(flowId);
    if (!flow) {
      sendToSocket(socket, { id: id || "oauth-submit", type: "error", error: "OAuth flow not found" });
      return;
    }
    try {
      if (
        flow.authorizationMode !== "browser" ||
        !flow.manualInputRequired ||
        !["starting", "pending"].includes(flow.status)
      ) {
        throw new Error("OAuth flow does not accept manual input");
      }
      _submitOAuthManualInput(flow, authorizationInput);
      sendToSocket(socket, {
        id,
        type: "oauth_submitted",
        flow_id: flow.id,
        provider: flow.provider,
        authorization_mode: flow.authorizationMode,
        user_code: flow.userCode,
        manual_input_required: flow.manualInputRequired,
        manual_input_prompt: flow.manualInputPrompt,
        manual_input_submitted: flow.manualInputSubmitted,
      });
    } catch (err) {
      sendToSocket(socket, {
        id: id || "oauth-submit",
        type: "error",
        error: err instanceof Error ? err.message : "Failed to submit OAuth input",
      });
    }
  }

  clearOAuthFlow(id, socket, flowId) {
    this._pruneExpiredOAuthFlows();
    if (typeof flowId !== "string" || !flowId) {
      sendToSocket(socket, { id: id || "oauth-clear", type: "error", error: "flowId is required" });
      return;
    }
    const flow = this.activeOAuthFlows.get(flowId);
    if (flow?.manualInputReject) {
      flow.manualInputReject(new Error("OAuth flow was cleared before manual input was consumed"));
    }
    this.activeOAuthFlows.delete(flowId);
    sendToSocket(socket, {
      id,
      type: "oauth_cleared",
      flow_id: flowId,
    });
  }

  async _createPiSession(
    sessionId,
    socket,
    modelRef,
    cwd,
    providerAuth,
    providerConfig,
    gitAuth,
    agentDir,
    importedSettings,
    normalizedPiSessionFile,
    orchestrationTools = [],
  ) {
    const { modelRuntime, registry } = await createModelRegistry(providerAuth, agentDir);
    const model = resolveModelFromRegistry(registry, modelRef, providerConfig);
    const {
      sessionManager,
      resetWarning,
      piSessionFile: openedPiSessionFile,
    } = openSessionManager(cwd, normalizedPiSessionFile);

    const settingsManager = SettingsManager.inMemory({
      compaction: { enabled: true },
      retry: { enabled: true, maxRetries: 3 },
    });
    const normalizedImportedSettings = normalizeImportedSettings(importedSettings);
    if (normalizedImportedSettings) {
      settingsManager.applyOverrides(normalizedImportedSettings);
    }

    const sessionOptions = {
      cwd,
      model,
      // The SDK's `tools` option is an allow-list of tool names. Pass Yinshi's
      // tool implementations as `customTools` so they replace the built-ins
      // while keeping read/bash/edit/write active for the model.
      customTools: [...createYinshiCodingTools(cwd, gitAuth), ...orchestrationTools],
      sessionManager,
      settingsManager,
      modelRuntime,
    };
    if (agentDir) {
      sessionOptions.agentDir = agentDir;
    }

    const { session } = await createAgentSession(sessionOptions);
    // Bind a web-friendly UI context so extensions (e.g. rtk-metrics) whose
    // handlers call ctx.ui.notify() can surface output in the chat. Without
    // this binding, notify() calls silently vanish in RPC mode.
    const runner = session.extensionRunner;
    console.log(`[sidecar] extension runner ${runner ? "available" : "unavailable"}`);
    if (runner) {
      runner.setUIContext(createWebUIContext(sessionId, socket, model));
      console.log("[sidecar] UI context bound");
    }
    if (resetWarning) {
      sendStatusToSocket(
        socket,
        sessionId,
        "context_reset",
        resetWarning,
        "warning",
      );
    }
    console.log("[sidecar] Pi session created");
    return { session, model, piSessionFile: openedPiSessionFile };
  }

  _disposePiSessionEntry(entry) {
    // A destroyed session can never receive backend answers; settle any
    // pending orchestration calls now instead of letting them time out.
    try {
      entry?.orchestration?.rpc?.dispose();
      if (entry?.orchestration) {
        entry.orchestration.rpc = null;
        entry.orchestration.socket = null;
      }
    } catch {
      // Disposal races must not block disposal of the pi session.
    }
    try {
      if (typeof entry?.unsubscribe === "function") {
        entry.unsubscribe();
      }
    } catch {
      // A failed unsubscribe must not block disposal of the pi session.
    }
    try {
      entry?.piSession?.dispose();
    } catch {
      // Disposal races are expected when a prompt is still unwinding.
    }
  }

  releasePiSession(sessionId) {
    const entry = this.activeSessions.get(sessionId);
    if (!entry) {
      return false;
    }
    this.activeSessions.delete(sessionId);
    this._disposePiSessionEntry(entry);
    return true;
  }

  _admitPiSessionCreation(sessionId) {
    if (
      this.activeSessions.has(sessionId)
      || this.pendingPiSessionCreations.has(sessionId)
    ) {
      return;
    }

    const reservedSessionIds = new Set([
      ...this.activeSessions.keys(),
      ...this.pendingPiSessionCreations.keys(),
    ]);
    while (reservedSessionIds.size >= PI_SESSION_COUNT_MAX) {
      const candidate = [...this.activeSessions.entries()]
        .filter(([activeSessionId]) => (
          !this._sessionHasActivePrompt(activeSessionId)
          && !this.pendingPiSessionCreations.has(activeSessionId)
        ))
        .sort((left, right) => (
          (left[1].lastActivityMs || 0) - (right[1].lastActivityMs || 0)
        ))[0];
      if (!candidate) {
        throw new Error("Pi session capacity reached");
      }
      const [candidateSessionId, candidateEntry] = candidate;
      this.activeSessions.delete(candidateSessionId);
      reservedSessionIds.delete(candidateSessionId);
      this._disposePiSessionEntry(candidateEntry);
    }
  }

  async _withPiSessionCreationLock(sessionId, operation) {
    this._admitPiSessionCreation(sessionId);
    const previous = this.pendingPiSessionCreations.get(sessionId) || Promise.resolve();
    let release;
    const current = new Promise((resolve) => {
      release = resolve;
    });
    this.pendingPiSessionCreations.set(sessionId, current);

    await previous;
    try {
      return await operation();
    } finally {
      release();
      if (this.pendingPiSessionCreations.get(sessionId) === current) {
        this.pendingPiSessionCreations.delete(sessionId);
      }
    }
  }

  async warmupSession(sessionId, socket, options) {
    const modelRef = options.model || DEFAULT_MODEL_REF;
    const cwd = options.cwd || process.cwd();
    const providerAuth = options.providerAuth || null;
    const providerConfig = options.providerConfig || null;
    const gitAuth = options.gitAuth || null;
    const agentDir = options.agentDir || null;
    const importedSettings = options.settings || null;

    try {
      await this._withPiSessionCreationLock(sessionId, async () => {
        if (this.activeSessions.has(sessionId)) {
          console.log("[sidecar] Pi session already active");
          return;
        }
        const requestedPiSessionFile = normalizePiSessionFile(options.piSessionFile || null);
        const {
          session: piSession,
          model,
          piSessionFile: normalizedPiSessionFile,
        } = await this._createPiSession(
          sessionId,
          socket,
          modelRef,
          cwd,
          providerAuth,
          providerConfig,
          gitAuth,
          agentDir,
          importedSettings,
          requestedPiSessionFile,
        );
        this.activeSessions.set(sessionId, {
          piSession,
          model,
          modelRef,
          cwd,
          providerAuth,
          providerConfig,
          gitAuth,
          importedSettings,
          piSessionFile: normalizedPiSessionFile,
          unsubscribe: null,
          cancelRequested: false,
          lastActivityMs: Date.now(),
          // Warmed sessions register no bridge tools. The registration flag
          // must exist so the first orchestration query recreates exactly
          // once, and plain queries never do.
          orchestration: { rpc: null },
          orchestrationRegistered: false,
        });
        console.log("[sidecar] Pi session warmed");
      });
      sendToSocket(socket, {
        id: sessionId,
        type: "warmup_status",
        success: true,
      });
    } catch {
      console.error("[sidecar] Warmup failed");
      sendToSocket(socket, {
        id: sessionId,
        type: "warmup_status",
        success: false,
        error: "Failed to warm up session",
      });
    }
  }

  async processQuery(sessionId, socket, prompt, options) {
    const bridgeRequested = Boolean(options.orchestration);
    const bridgeActive = [...this.activePromptSessionsBySocket.values()].some(
      sessions => [...(sessions.get(sessionId) || [])].some(state => state.bridgeEnabled),
    );
    if (this._sessionHasActivePrompt(sessionId) && (bridgeRequested || bridgeActive)) {
      sendToSocket(socket, { id: sessionId, type: "error", error: "The session already has an active query." });
      return;
    }
    const promptState = this._trackPromptSession(socket, sessionId);
    promptState.bridgeEnabled = bridgeRequested;
    const modelRef = options.model || DEFAULT_MODEL_REF;
    const cwd = options.cwd || process.cwd();
    const providerAuth = options.providerAuth || null;
    const providerConfig = options.providerConfig || null;
    const gitAuth = options.gitAuth || null;
    const agentDir = options.agentDir || null;
    const importedSettings = options.settings || null;
    let entry = this.activeSessions.get(sessionId);
    let clearFinalizeTimer = () => {};
    console.log(`[sidecar] Prompt accepted warm=${Boolean(entry)}`);

    try {
      const requestedPiSessionFile = normalizePiSessionFile(options.piSessionFile || null);
      const orchestration = options.orchestration || null;
      const orchestrationCapability =
        orchestration && typeof orchestration.capability === "string"
          ? orchestration.capability
          : null;
      const protocolVersion = orchestration?.protocol_version ?? 1;
      const allowedOperations = protocolVersion === 2 ? orchestration?.allowed_operations : ["ping_thread_bridge"];
      const optionFields = protocolVersion === 2
        ? ["capability", "protocol_version", "allowed_operations"] : ["capability"];
      if (orchestration && (
        typeof orchestration !== "object" || Array.isArray(orchestration)
        || ![1, 2].includes(protocolVersion)
        || Object.keys(orchestration).length !== optionFields.length
        || Object.keys(orchestration).some(key => !optionFields.includes(key))
        || !Array.isArray(allowedOperations) || allowedOperations.length === 0
        || new Set(allowedOperations).size !== allowedOperations.length
        || (protocolVersion === 2 && allowedOperations.some(name => !THREAD_OPERATIONS.includes(name)))
        || !orchestrationCapability || orchestrationCapability.length > 256
        || !/^[\x21-\x7e]+$/.test(orchestrationCapability)
      )) {
        throw new Error("Orchestration options require one bounded capability string.");
      }
      const wantOrchestrationTools = Boolean(orchestrationCapability);
      const permissions = !wantOrchestrationTools ? "none"
        : protocolVersion === 1 ? "legacy" : JSON.stringify([...allowedOperations].sort());
      entry = await this._withPiSessionCreationLock(sessionId, async () => {
        const currentEntry = this.activeSessions.get(sessionId);
        const authChanged = JSON.stringify(currentEntry?.providerAuth || null)
          !== JSON.stringify(providerAuth);
        const configChanged = JSON.stringify(currentEntry?.providerConfig || null)
          !== JSON.stringify(providerConfig);
        const gitAuthChanged = JSON.stringify(currentEntry?.gitAuth || null)
          !== JSON.stringify(gitAuth);
        const settingsChanged = JSON.stringify(currentEntry?.importedSettings || null)
          !== JSON.stringify(importedSettings);
        const piSessionFileChanged = (currentEntry?.piSessionFile || null)
          !== requestedPiSessionFile;
        // Conditional tool registration: sessions created without bridge
        // tools must be recreated before the first orchestration query so
        // the model never sees a stale or missing tool set.
        const orchestrationRegistrationChanged = Boolean(
          currentEntry
          && (currentEntry.orchestrationPermissions
            ?? (currentEntry.orchestrationRegistered ? "legacy" : "none")) !== permissions,
        );
        if (
          currentEntry
          && currentEntry.modelRef === modelRef
          && !authChanged
          && !configChanged
          && !gitAuthChanged
          && !settingsChanged
          && !piSessionFileChanged
          && !orchestrationRegistrationChanged
        ) {
          return currentEntry;
        }

        if (currentEntry) {
          this.activeSessions.delete(sessionId);
          this._disposePiSessionEntry(currentEntry);
        }
        const orchestrationState = { rpc: null };
        const rpcForCall = () => orchestrationState.rpc;
        const orchestrationTools = !wantOrchestrationTools ? []
          : protocolVersion === 2
            ? createThreadTools({ allowedOperations, rpcForCall })
            : [createThreadBridgePingTool({ rpcForCall })];
        const {
          session: piSession,
          model,
          piSessionFile: normalizedPiSessionFile,
        } = await this._createPiSession(
          sessionId,
          socket,
          modelRef,
          cwd,
          providerAuth,
          providerConfig,
          gitAuth,
          agentDir,
          importedSettings,
          requestedPiSessionFile,
          orchestrationTools,
        );
        const createdEntry = {
          piSession,
          model,
          modelRef,
          cwd,
          providerAuth,
          providerConfig,
          gitAuth,
          importedSettings,
          piSessionFile: normalizedPiSessionFile,
          unsubscribe: null,
          cancelRequested: false,
          lastActivityMs: Date.now(),
          orchestration: orchestrationState,
          orchestrationRegistered: wantOrchestrationTools,
          orchestrationPermissions: permissions,
        };
        this.activeSessions.set(sessionId, createdEntry);
        return createdEntry;
      });

      if (promptState.cancelled) {
        return;
      }

      // Activate the per-query orchestration channel for this prompt only.
      // The capability lives in memory for the duration of the query and is
      // dropped afterwards, so a reused Pi session never keeps a stale one.
      // The owning socket is recorded so responses from any other socket
      // are refused.
      if (orchestrationCapability && entry.orchestration) {
        if (entry.orchestration.rpc) {
          entry.orchestration.rpc.dispose();
        }
        entry.orchestration.rpc = createOrchestrationRpc({
          sessionId,
          capability: orchestrationCapability,
          protocolVersion,
          allowedOperations,
          send: (frame) => sendToSocket(socket, frame),
        });
        entry.orchestration.socket = socket;
      }

      const { piSession, model } = entry;

      if (entry.unsubscribe) {
        entry.unsubscribe();
      }

      let usage = null;
      // When pi handles a prompt as an extension command (text starting with
      // "/" that matches a registered command), it returns from prompt()
      // without firing "agent_end". The stream would hang forever waiting
      // for a "result" event. Track whether agent_end fired so we can emit
      // a synthetic one after prompt() resolves.
      let agentEndEmitted = false;
      let resultSent = false;
      let pendingResult = null;
      let compactionActive = false;
      let finalizeTimer = null;

      const buildResultMessage = (resultUsage) => ({
        id: sessionId,
        type: "message",
        data: {
          type: "result",
          usage: resultUsage || {},
          provider: model.provider,
          model: `${model.provider}/${model.id}`,
        },
      });

      clearFinalizeTimer = () => {
        if (finalizeTimer) {
          clearTimeout(finalizeTimer);
          finalizeTimer = null;
        }
      };

      const schedulePendingResult = () => {
        clearFinalizeTimer();
        finalizeTimer = setTimeout(() => {
          finalizeTimer = null;
          if (resultSent || compactionActive || !pendingResult) {
            return;
          }
          sendToSocket(socket, pendingResult);
          pendingResult = null;
          resultSent = true;
        }, 0);
      };

      entry.unsubscribe = piSession.subscribe((event) => {
        console.log(`[sidecar][event] type=${event.type}`);
        switch (event.type) {
          case "message_update": {
            const assistantEvent = event.assistantMessageEvent;
            if (assistantEvent.type === "text_delta") {
              sendToSocket(socket, {
                id: sessionId,
                type: "message",
                data: {
                  type: "assistant",
                  message: {
                    content: [{ type: "text", text: assistantEvent.delta }],
                  },
                },
              });
            } else if (assistantEvent.type === "thinking_delta") {
              sendToSocket(socket, {
                id: sessionId,
                type: "message",
                data: {
                  type: "assistant",
                  message: {
                    content: [
                      { type: "thinking", thinking: assistantEvent.delta },
                    ],
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
                id: event.toolCallId,
                toolName: event.toolName,
                toolInput: event.args,
              },
            });
            break;
          case "tool_execution_update":
            sendToSocket(socket, {
              id: sessionId,
              type: "tool_result",
              tool_use_id: event.toolCallId,
              content: stringifyToolResult(event.partialResult),
              partial: true,
            });
            break;
          case "tool_execution_end":
            sendToSocket(socket, {
              id: sessionId,
              type: "tool_result",
              tool_use_id: event.toolCallId,
              content: stringifyToolResult(event.result),
              is_error: event.isError === true,
            });
            break;
          case "turn_end":
            if (event.message && event.message.usage) {
              const eventUsage = event.message.usage;
              usage = {
                input_tokens: eventUsage.input || 0,
                output_tokens: eventUsage.output || 0,
                cache_read_input_tokens: eventUsage.cacheRead || 0,
                cache_creation_input_tokens: eventUsage.cacheWrite || 0,
              };
            }
            break;
          case "agent_end":
            pendingResult = buildResultMessage(usage);
            usage = null;
            agentEndEmitted = true;
            schedulePendingResult();
            break;
          case "auto_retry_start":
            pendingResult = null;
            clearFinalizeTimer();
            console.log(
              `[sidecar] Retrying provider request (attempt ${event.attempt}/${event.maxAttempts})`,
            );
            sendStatusToSocket(
              socket,
              sessionId,
              "retrying",
              `Retrying after provider error (attempt ${event.attempt}/${event.maxAttempts})...`,
            );
            break;
          case "auto_retry_end":
            sendStatusToSocket(
              socket,
              sessionId,
              event.success ? "retry_complete" : "retry_failed",
              event.success ? "Retry complete." : "Retry failed.",
              event.success ? "info" : "warning",
            );
            break;
          case "compaction_start":
            compactionActive = true;
            clearFinalizeTimer();
            console.log("[sidecar] Compacting context...");
            sendStatusToSocket(
              socket,
              sessionId,
              "compacting",
              "Compacting context...",
              "info",
              { reason: event.reason },
            );
            break;
          case "compaction_end":
            compactionActive = false;
            if (event.willRetry) {
              pendingResult = null;
            }
            sendStatusToSocket(
              socket,
              sessionId,
              event.errorMessage ? "compaction_failed" : "compacted",
              event.errorMessage || "Context compacted.",
              event.errorMessage ? "warning" : "info",
              {
                reason: event.reason,
                willRetry: event.willRetry === true,
                aborted: event.aborted === true,
              },
            );
            if (!event.willRetry) {
              schedulePendingResult();
            }
            break;
        }
      });

      console.log("[sidecar] Prompt started");
      promptState.executionStarted = true;
      await piSession.prompt(prompt);
      console.log("[sidecar] Prompt ended");
      // Clear cancelRequested after normal completion
      entry.cancelRequested = false;

      // Pi returns from prompt() without firing agent_end when it handles an
      // extension command inline (e.g. `/rtk-stats`). Synthesise the result
      // event so the client stream loop terminates cleanly instead of hanging.
      if (!agentEndEmitted && !resultSent) {
        console.log("[sidecar] Synthesising inline-command result");
        sendToSocket(socket, buildResultMessage(usage));
        resultSent = true;
      }
    } catch (err) {
      clearFinalizeTimer();
      const errorMessage = err instanceof Error ? err.message : String(err);
      if (entry?.cancelRequested) {
        console.log("[sidecar] Prompt cancelled by user");
        sendToSocket(socket, {
          id: sessionId,
          type: "cancelled",
        });
        // Clear cancelRequested after handling cancellation
        entry.cancelRequested = false;
      } else {
        console.error("[sidecar] Prompt failed");
        sendToSocket(socket, {
          id: sessionId,
          type: "error",
          error: errorMessage,
        });
      }
    } finally {
      if (entry?.orchestration?.rpc) {
        // Dispose first so every pending request settles before the channel
        // reference is dropped.
        entry.orchestration.rpc.dispose();
        entry.orchestration.rpc = null;
      }
      if (entry?.orchestration) {
        entry.orchestration.socket = null;
      }
      this._untrackPromptSession(socket, promptState);
    }
  }

  async handleCancelRequest(sessionId, socket) {
    try {
      await this.cancelSession(sessionId);
      sendToSocket(socket, {
        id: sessionId,
        type: "cancel_status",
        success: true,
      });
    } catch {
      console.error("[sidecar] Cancellation failed");
      sendToSocket(socket, {
        id: sessionId,
        type: "cancel_status",
        success: false,
        error: "Failed to cancel session",
      });
    }
  }

  async cancelSession(sessionId) {
    const entry = this.activeSessions.get(sessionId);
    if (!entry) {
      console.log("[sidecar] Cancellation target not found");
      return;
    }
    console.log("[sidecar] Cancelling prompt");
    entry.cancelRequested = true;
    // Pending backend calls can never be answered by an aborted prompt.
    // Dispose the channel immediately so tools waiting on a response settle
    // now instead of deadlocking until the abort unwinds.
    if (entry.orchestration?.rpc) {
      entry.orchestration.rpc.dispose();
    }
    try {
      entry.piSession.abortCompaction();
      entry.piSession.abortRetry();
      await entry.piSession.abort();
    } catch (error) {
      entry.cancelRequested = false;
      throw error;
    }
  }

  cleanup() {
    try {
      if (fs.existsSync(this.socketPath)) {
        fs.unlinkSync(this.socketPath);
      }
    } catch {
      // ignore cleanup races
    }

    if (this.server) {
      try {
        this.server.close();
      } catch {
        // ignore cleanup races
      }
    }

    for (const [, entry] of this.activeSessions) {
      try {
        if (entry.unsubscribe) {
          entry.unsubscribe();
        }
        entry.piSession.dispose();
      } catch {
        // ignore cleanup races
      }
    }
    this.activeSessions.clear();
    this.activePromptSessionsBySocket.clear();
    this.pendingPiSessionCreations.clear();
    this.activeOAuthFlows.clear();
    for (const [, entry] of this.activeTerminals) {
      try {
        entry.terminal.kill();
      } catch {
        // ignore cleanup races
      }
    }
    this.activeTerminals.clear();

    if (this.healthCheckInterval) {
      clearInterval(this.healthCheckInterval);
      this.healthCheckInterval = null;
    }
  }
}
