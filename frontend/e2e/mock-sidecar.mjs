import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import path from "node:path";
import readline from "node:readline";

const socketPath = process.env.SIDECAR_SOCKET_PATH;
const healthPort = Number(process.env.MOCK_SIDECAR_HEALTH_PORT || "9777");

if (!socketPath) {
  throw new Error("SIDECAR_SOCKET_PATH is required");
}

fs.mkdirSync(path.dirname(socketPath), { recursive: true });
fs.rmSync(socketPath, { force: true });

function resolveModel(modelKey = "minimax-m2.7") {
  const key = String(modelKey).trim();
  const lowerKey = key.toLowerCase();
  if (lowerKey === "sonnet" || lowerKey.includes("anthropic/") || lowerKey.includes("claude")) {
    return {
      provider: "anthropic",
      model: "anthropic/claude-sonnet-4-20250514",
    };
  }

  if (lowerKey.includes("openai/") || lowerKey.includes("gpt")) {
    return {
      provider: "openai",
      model: "openai/gpt-4o-mini",
    };
  }

  if (lowerKey.includes("highspeed")) {
    return {
      provider: "minimax",
      model: "minimax/MiniMax-M2.7-highspeed",
    };
  }

  return {
    provider: "minimax",
    model: "minimax/MiniMax-M2.7",
  };
}

const catalog = {
  default_model: "minimax/MiniMax-M2.7",
  providers: [
    { id: "anthropic", model_count: 1 },
    { id: "minimax", model_count: 2 },
    { id: "openai", model_count: 1 },
  ],
  models: [
    {
      ref: "anthropic/claude-sonnet-4-20250514",
      provider: "anthropic",
      id: "claude-sonnet-4-20250514",
      label: "Claude Sonnet 4",
      api: "anthropic-messages",
      reasoning: true,
      inputs: ["text", "image"],
      context_window: 200000,
      max_tokens: 16384,
    },
    {
      ref: "minimax/MiniMax-M2.7",
      provider: "minimax",
      id: "MiniMax-M2.7",
      label: "MiniMax M2.7",
      api: "openai-completions",
      reasoning: false,
      inputs: ["text"],
      context_window: 200000,
      max_tokens: 16384,
    },
    {
      ref: "minimax/MiniMax-M2.7-highspeed",
      provider: "minimax",
      id: "MiniMax-M2.7-highspeed",
      label: "MiniMax M2.7 Highspeed",
      api: "openai-completions",
      reasoning: false,
      inputs: ["text"],
      context_window: 200000,
      max_tokens: 16384,
    },
    {
      ref: "openai/gpt-4o-mini",
      provider: "openai",
      id: "gpt-4o-mini",
      label: "GPT-4o Mini",
      api: "openai-responses",
      reasoning: false,
      inputs: ["text", "image"],
      context_window: 128000,
      max_tokens: 16384,
    },
  ],
};

const sessionOptions = new Map();

const server = net.createServer((socket) => {
  socket.write(`${JSON.stringify({ type: "init_status", success: true })}\n`);

  const lines = readline.createInterface({ input: socket });
  lines.on("line", (line) => {
    const message = JSON.parse(line);

    if (message.type === "ping") {
      socket.write(`${JSON.stringify({ type: "pong" })}\n`);
      return;
    }

    if (message.type === "resolve") {
      const resolvedModel = resolveModel(message.model);
      socket.write(
        `${JSON.stringify({
          type: "resolved",
          id: message.id,
          provider: resolvedModel.provider,
          model: resolvedModel.model,
        })}\n`,
      );
      return;
    }

    if (message.type === "catalog") {
      socket.write(
        `${JSON.stringify({
          type: "catalog",
          id: message.id,
          ...catalog,
        })}\n`,
      );
      return;
    }

    if (message.type === "auth_resolve") {
      const authStrategy = message.providerAuth?.authStrategy;
      const secret = message.providerAuth?.secret ?? null;
      let runtimeApiKey = null;
      if (authStrategy === "oauth") {
        if (secret && typeof secret === "object" && typeof secret.accessToken === "string") {
          runtimeApiKey = secret.accessToken;
        } else {
          runtimeApiKey = "oauth-runtime-key";
        }
      } else if (typeof secret === "string") {
        runtimeApiKey = secret;
      }
      socket.write(
        `${JSON.stringify({
          type: "auth_resolved",
          id: message.id,
          provider: message.provider,
          auth: secret,
          model_ref: resolveModel(message.model).model,
          runtime_api_key: runtimeApiKey,
          model_config: null,
        })}\n`,
      );
      return;
    }

    if (message.type === "warmup") {
      sessionOptions.set(message.id, message.options ?? {});
      return;
    }

    if (message.type === "cancel") {
      socket.write(
        `${JSON.stringify({ type: "error", id: message.id, error: "cancelled" })}\n`,
      );
      return;
    }

    if (message.type === "query") {
      const options = sessionOptions.get(message.id) ?? message.options ?? {};
      const provider = resolveModel(options.model ?? "minimax/MiniMax-M2.7").provider;
      const prompt = String(message.prompt ?? "");
      const assistantEvent = {
        type: "message",
        id: message.id,
        data: {
          type: "assistant",
          message: {
            content: [{ type: "text", text: `Mock reply for: ${prompt}` }],
          },
        },
      };
      const resultEvent = {
        type: "message",
        id: message.id,
        data: {
          type: "result",
          usage: { input_tokens: 100, output_tokens: 50 },
          provider,
        },
      };

      setTimeout(() => {
        socket.write(`${JSON.stringify(assistantEvent)}\n`);
      }, 40);
      setTimeout(() => {
        socket.write(`${JSON.stringify(resultEvent)}\n`);
      }, 120);
    }
  });
});

const healthServer = http.createServer((request, response) => {
  response.writeHead(200, { "content-type": "text/plain" });
  response.end("ok");
});

server.listen(socketPath, () => {
  process.stdout.write(`mock-sidecar listening on ${socketPath}\n`);
});
healthServer.listen(healthPort);

function shutdown() {
  server.close(() => {
    healthServer.close(() => {
      fs.rmSync(socketPath, { force: true });
      process.exit(0);
    });
  });
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
