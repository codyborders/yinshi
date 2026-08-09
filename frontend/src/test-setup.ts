import "@testing-library/jest-dom/vitest";

function installStorageMock(storageName: "localStorage" | "sessionStorage"): void {
  const existingStorage = globalThis[storageName];
  const hasStorageApi =
    typeof existingStorage === "object" &&
    existingStorage !== null &&
    typeof existingStorage.getItem === "function" &&
    typeof existingStorage.setItem === "function" &&
    typeof existingStorage.removeItem === "function" &&
    typeof existingStorage.clear === "function";
  if (hasStorageApi) {
    return;
  }

  const values = new Map<string, string>();
  const storageMock = {
    get length(): number {
      return values.size;
    },
    clear(): void {
      values.clear();
    },
    getItem(key: string): string | null {
      return values.get(String(key)) ?? null;
    },
    key(index: number): string | null {
      const keys = Array.from(values.keys());
      return keys[index] ?? null;
    },
    removeItem(key: string): void {
      values.delete(String(key));
    },
    setItem(key: string, value: string): void {
      values.set(String(key), String(value));
    },
  };

  Object.defineProperty(globalThis, storageName, {
    configurable: true,
    value: storageMock,
  });
}

installStorageMock("localStorage");
installStorageMock("sessionStorage");
