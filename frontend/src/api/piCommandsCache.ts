import { ApiError, api, type PiConfigCommands } from "./client";
import type { SlashCommand } from "../components/SlashCommandMenu";
import type { RuntimeTransport } from "../runtime/runtimeTransport";

// Module-level cache shared across every Session mount. Without this, every
// session navigation triggers a new HTTP request which triggers the sidecar
// to re-evaluate every extension module (moduleCache:false in pi-mono).
const cachedPromises = new Map<string, Promise<SlashCommand[]>>();
const subscribers = new Set<() => void>();

function toSlashCommand(command: PiConfigCommands["commands"][number]): SlashCommand {
  return {
    name: command.command_name,
    description: command.description,
    source: "pi",
  };
}

function transportKey(runtimeTransport?: RuntimeTransport): string {
  const runtime = runtimeTransport?.runtime;
  if (!runtime) return "default";
  return runtime.location === "byoc" ? `byoc:${runtime.runnerId}` : runtime.location;
}

async function fetchCommands(runtimeTransport?: RuntimeTransport): Promise<SlashCommand[]> {
  try {
    const commandClient = runtimeTransport ?? api;
    const payload = await commandClient.get<PiConfigCommands>(
      "/api/settings/pi-config/commands",
    );
    return payload.commands.map(toSlashCommand);
  } catch (error) {
    // A missing Pi config has no imported commands. A 503 is different: the
    // sidecar or tenant container is still warming up, so callers must retry
    // rather than cache an empty palette for the rest of the browser session.
    if (error instanceof ApiError && error.status === 404) {
      return [];
    }
    throw error;
  }
}

export function getCachedPiCommands(
  runtimeTransport?: RuntimeTransport,
): Promise<SlashCommand[]> {
  const key = transportKey(runtimeTransport);
  const cachedPromise = cachedPromises.get(key);
  if (cachedPromise) return cachedPromise;
  const promise = fetchCommands(runtimeTransport).catch((error) => {
    // Clear only this location after failure so another runtime's cache survives.
    if (cachedPromises.get(key) === promise) {
      cachedPromises.delete(key);
    }
    throw error;
  });
  cachedPromises.set(key, promise);
  return promise;
}

export function invalidatePiCommands(runtimeTransport?: RuntimeTransport): void {
  if (runtimeTransport) {
    cachedPromises.delete(transportKey(runtimeTransport));
  } else {
    cachedPromises.clear();
  }
  for (const notify of subscribers) {
    notify();
  }
}

export function subscribePiCommands(notify: () => void): () => void {
  subscribers.add(notify);
  return () => {
    subscribers.delete(notify);
  };
}
