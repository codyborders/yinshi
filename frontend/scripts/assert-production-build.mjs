import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const distDirectory = new URL("../dist/", import.meta.url);
const indexHtml = await readFile(new URL("index.html", distDirectory), "utf8");
const moduleEntries = Array.from(
  indexHtml.matchAll(
    /<script\s+type="module"[^>]+src="([^"]+)"[^>]*><\/script>/g,
  ),
  (match) => match[1],
);

if (
  moduleEntries.length !== 1 ||
  !/^\/assets\/[A-Za-z0-9_-]+\.js$/.test(moduleEntries[0])
) {
  throw new Error(
    `Production index must directly load one generated module entry: ${JSON.stringify(moduleEntries)}`,
  );
}
if (/\.wasm|type="application\/wasm"/i.test(indexHtml)) {
  throw new Error("Production index must not preload Noise WASM");
}
if (/\/assets\/preload-[A-Za-z0-9_-]+\.js/.test(indexHtml)) {
  throw new Error("Production index must not load an early preload module");
}

async function findWasmFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) files.push(...(await findWasmFiles(path)));
    else if (entry.isFile() && entry.name.endsWith(".wasm")) files.push(path);
  }
  return files;
}

const wasmFiles = await findWasmFiles(fileURLToPath(distDirectory));
if (wasmFiles.length !== 1) {
  throw new Error(
    `Production build must contain one lazy Noise WASM payload: ${JSON.stringify(wasmFiles)}`,
  );
}

console.log(
  `Production build entry ${moduleEntries[0]} has no WASM preload and retains ${wasmFiles.length} lazy WASM payload.`,
);
