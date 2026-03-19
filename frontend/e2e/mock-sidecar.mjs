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
  const key = String(modelKey).toLowerCase();
  if (key.includes("sonnet") || key.includes("claude")) {
    return {
      provider: "anthropic",
      model: "claude-sonnet-4-20250514",
    };
  }

  if (key.includes("highspeed")) {
    return {
      provider: "minimax",
      model: "MiniMax-M2.7-highspeed",
    };
  }

  return {
    provider: "minimax",
    model: "MiniMax-M2.7",
  };
}

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
      socket.write(
        `${JSON.stringify({
          type: "resolved",
          id: message.id,
          ...resolveModel(message.model),
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
      const provider = resolveModel(options.model ?? "minimax-m2.7").provider;
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
