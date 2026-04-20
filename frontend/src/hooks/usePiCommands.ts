import { useEffect, useState } from "react";

import {
  getCachedPiCommands,
  subscribePiCommands,
} from "../api/piCommandsCache";
import type { SlashCommand } from "../components/SlashCommandMenu";

const EMPTY_COMMANDS: SlashCommand[] = [];

export function usePiCommands(): SlashCommand[] {
  const [commands, setCommands] = useState<SlashCommand[]>(EMPTY_COMMANDS);

  useEffect(() => {
    let disposed = false;

    async function load(): Promise<void> {
      try {
        const loaded = await getCachedPiCommands();
        if (!disposed) {
          setCommands(loaded);
        }
      } catch {
        // Network/server failure -- fall back to no pi commands rather than
        // block the palette entirely. Concrete error surfaces are handled by
        // adjacent UI (catalog, settings page).
        if (!disposed) {
          setCommands(EMPTY_COMMANDS);
        }
      }
    }

    void load();
    const unsubscribe = subscribePiCommands(() => {
      void load();
    });

    return () => {
      disposed = true;
      unsubscribe();
    };
  }, []);

  return commands;
}
