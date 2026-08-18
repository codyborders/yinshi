import { DEFAULT_SESSION_MODEL } from "./sessionModels";

const SESSION_MODEL_STORAGE_PREFIX = "yinshi:last-session-model:";
const LOCAL_USER_SCOPE = "local";
const MODEL_REF_PATTERN = /^\S+\/\S+$/;
const MODEL_REF_LENGTH_MAX = 100;

function preferenceStorageKey(userId: string | null): string {
  const normalizedUserId = userId?.trim();
  const userScope = normalizedUserId || LOCAL_USER_SCOPE;
  return `${SESSION_MODEL_STORAGE_PREFIX}${userScope}`;
}

function normalizeModelRef(model: string | null): string | null {
  const normalizedModel = model?.trim();
  if (!normalizedModel || normalizedModel.length > MODEL_REF_LENGTH_MAX) {
    return null;
  }
  if (!MODEL_REF_PATTERN.test(normalizedModel)) {
    return null;
  }
  return normalizedModel;
}

export function preferredSessionModel(userId: string | null): string {
  if (typeof window === "undefined") {
    return DEFAULT_SESSION_MODEL;
  }
  try {
    return (
      normalizeModelRef(localStorage.getItem(preferenceStorageKey(userId))) ||
      DEFAULT_SESSION_MODEL
    );
  } catch {
    return DEFAULT_SESSION_MODEL;
  }
}

export function rememberSessionModel(
  userId: string | null,
  model: string,
): void {
  const normalizedModel = normalizeModelRef(model);
  if (!normalizedModel || typeof window === "undefined") {
    return;
  }
  try {
    localStorage.setItem(preferenceStorageKey(userId), normalizedModel);
  } catch {
    return;
  }
}
