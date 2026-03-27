import type { ModelDescriptor } from "../api/client";

export const DEFAULT_SESSION_MODEL = "minimax/MiniMax-M2.7";

const LEGACY_MODEL_ALIASES = new Map<string, string>([
  ["haiku", "anthropic/claude-haiku-4-5-20251001"],
  ["minimax", DEFAULT_SESSION_MODEL],
  ["minimax-m2.5-highspeed", "minimax/MiniMax-M2.5-highspeed"],
  ["minimax-m2.7", DEFAULT_SESSION_MODEL],
  ["minimax-m2.7-highspeed", "minimax/MiniMax-M2.7-highspeed"],
  ["opus", "anthropic/claude-opus-4-20250514"],
  ["sonnet", "anthropic/claude-sonnet-4-20250514"],
]);

function normalizeModelValue(value: string): string {
  return value.trim().toLowerCase();
}

export function resolveSessionModelKey(
  model: string,
  models: ModelDescriptor[],
): string | null {
  const normalizedModel = normalizeModelValue(model);
  if (!normalizedModel) {
    return null;
  }
  const aliasMatch = LEGACY_MODEL_ALIASES.get(normalizedModel);
  if (aliasMatch) {
    return aliasMatch;
  }
  const matchingModel = models.find((candidate) => {
    if (normalizeModelValue(candidate.ref) === normalizedModel) {
      return true;
    }
    if (normalizeModelValue(candidate.id) === normalizedModel) {
      return true;
    }
    return normalizeModelValue(candidate.label) === normalizedModel;
  });
  return matchingModel ? matchingModel.ref : null;
}

export function getSessionModelOption(
  model: string | null | undefined,
  models: ModelDescriptor[],
): ModelDescriptor | null {
  if (!model) {
    return null;
  }
  const resolvedKey = resolveSessionModelKey(model, models);
  if (!resolvedKey) {
    return null;
  }
  return models.find((candidate) => candidate.ref === resolvedKey) || null;
}

export function getSessionModelLabel(
  model: string,
  models: ModelDescriptor[],
): string {
  const matchingModel = getSessionModelOption(model, models);
  if (!matchingModel) {
    return model;
  }
  return matchingModel.label;
}

export function describeSessionModel(
  model: string,
  models: ModelDescriptor[],
): string {
  const matchingModel = getSessionModelOption(model, models);
  if (!matchingModel) {
    return `\`${model}\``;
  }
  return `**${matchingModel.label}** (\`${matchingModel.ref}\`)`;
}

export function availableSessionModelsMarkdown(models: ModelDescriptor[]): string {
  return models
    .map((model) => `- **${model.label}** (\`${model.ref}\`)`)
    .join("\n");
}
