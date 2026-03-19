export interface SessionModelOption {
  value: string;
  label: string;
  aliases: string[];
}

export const DEFAULT_SESSION_MODEL = "minimax-m2.7";

export const SESSION_MODEL_OPTIONS: SessionModelOption[] = [
  {
    value: "minimax-m2.7",
    label: "MiniMax M2.7",
    aliases: ["minimax", "MiniMax-M2.7"],
  },
  {
    value: "minimax-m2.7-highspeed",
    label: "MiniMax M2.7 Highspeed",
    aliases: ["MiniMax-M2.7-highspeed", "Minimax-M2.7-highspeed"],
  },
  {
    value: "sonnet",
    label: "Claude Sonnet 4",
    aliases: ["claude-sonnet-4-20250514"],
  },
  {
    value: "opus",
    label: "Claude Opus 4",
    aliases: ["claude-opus-4-20250514"],
  },
  {
    value: "haiku",
    label: "Claude Haiku 4.5",
    aliases: ["claude-haiku-4-5-20251001"],
  },
];

function normalizeModelValue(value: string): string {
  const trimmedValue = value.trim();
  return trimmedValue.toLowerCase();
}

export function resolveSessionModelKey(model: string): string | null {
  const normalizedModel = normalizeModelValue(model);
  if (!normalizedModel) {
    return null;
  }

  const matchingOption = SESSION_MODEL_OPTIONS.find((option) => {
    if (normalizeModelValue(option.value) === normalizedModel) {
      return true;
    }
    if (normalizeModelValue(option.label) === normalizedModel) {
      return true;
    }
    return option.aliases.some(
      (alias) => normalizeModelValue(alias) === normalizedModel,
    );
  });
  if (!matchingOption) {
    return null;
  }

  return matchingOption.value;
}

export function getSessionModelOption(
  model: string | null | undefined,
): SessionModelOption | null {
  if (!model) {
    return null;
  }

  const resolvedKey = resolveSessionModelKey(model);
  if (!resolvedKey) {
    return null;
  }

  const matchingOption = SESSION_MODEL_OPTIONS.find(
    (option) => option.value === resolvedKey,
  );
  if (!matchingOption) {
    return null;
  }

  return matchingOption;
}

export function getSessionModelLabel(model: string): string {
  const matchingOption = getSessionModelOption(model);
  if (!matchingOption) {
    return model;
  }

  return matchingOption.label;
}

export function describeSessionModel(model: string): string {
  const matchingOption = getSessionModelOption(model);
  if (!matchingOption) {
    return `\`${model}\``;
  }

  return `**${matchingOption.label}** (\`${matchingOption.value}\`)`;
}

export function availableSessionModelsMarkdown(): string {
  return SESSION_MODEL_OPTIONS.map(
    (option) => `- **${option.label}** (\`${option.value}\`)`,
  ).join("\n");
}
