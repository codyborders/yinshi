import { ApiError, api, type PiConfigCommands } from "./client";
import type { SlashCommand } from "../components/SlashCommandMenu";

// Module-level cache shared across every Session mount. Without this, every
// session navigation triggers a new HTTP request which triggers the sidecar
// to re-evaluate every extension module (moduleCache:false in pi-mono).
let cachedPromise: Promise<SlashCommand[]> | null = null;
const subscribers = new Set<() => void>();

function toSlashCommand(command: PiConfigCommands["commands"][number]): SlashCommand {
  return {
    name: command.command_name,
    description: command.description,
    source: "pi",
  };
}

async function fetchCommands(): Promise<SlashCommand[]> {
  try {
    const payload = await api.get<PiConfigCommands>(
      "/api/settings/pi-config/commands",
    );
    return payload.commands.map(toSlashCommand);
  } catch (error) {
    // 503 from an unready sidecar and 404 from a missing config both mean
    // "no commands right now"; surface as empty rather than a hard error.
    if (
      error instanceof ApiError &&
      (error.status === 503 || error.status === 404)
    ) {
      return [];
    }
    throw error;
  }
}

export function getCachedPiCommands(): Promise<SlashCommand[]> {
  if (cachedPromise === null) {
    cachedPromise = fetchCommands().catch((error) => {
      // Clear the cached promise on failure so the next caller can retry;
      // leaving a rejected promise cached would permanently break the palette.
      cachedPromise = null;
      throw error;
    });
  }
  return cachedPromise;
}

export function invalidatePiCommands(): void {
  cachedPromise = null;
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
