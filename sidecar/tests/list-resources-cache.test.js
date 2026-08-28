import assert from "node:assert/strict";
import test from "node:test";

test("resource-list cache is bounded with deterministic LRU eviction", async () => {
  const sidecarModule = await import("../src/sidecar.js");
  const { LIST_RESOURCES_CACHE_MAX, createBoundedCache } = sidecarModule;

  assert.equal(typeof createBoundedCache, "function");
  assert.ok(Number.isSafeInteger(LIST_RESOURCES_CACHE_MAX) && LIST_RESOURCES_CACHE_MAX > 0);
  // The production cache instance stays private. Tests instantiate their own.
  assert.equal(sidecarModule.resourceListCache, undefined);

  const isolatedCache = createBoundedCache(2);
  isolatedCache.set("first", { value: 1 });
  isolatedCache.set("second", { value: 2 });
  assert.equal(isolatedCache.get("first").value, 1);
  isolatedCache.set("third", { value: 3 });
  assert.equal(isolatedCache.get("second"), undefined);
  assert.equal(isolatedCache.get("first").value, 1);
  assert.equal(isolatedCache.get("third").value, 3);
  assert.equal(isolatedCache.size(), 2);
  isolatedCache.set("second-again", { value: 22 });
  assert.equal(isolatedCache.get("first"), undefined);
  assert.equal(isolatedCache.size(), 2);

  const productionSizedCache = createBoundedCache(LIST_RESOURCES_CACHE_MAX);
  const keys = Array.from(
    { length: LIST_RESOURCES_CACHE_MAX + 1 },
    (_unused, index) => `dir-${index}`,
  );
  for (const key of keys.slice(0, LIST_RESOURCES_CACHE_MAX)) {
    productionSizedCache.set(key, { value: key });
  }
  assert.equal(productionSizedCache.get(keys[0]).value, keys[0]);
  productionSizedCache.set(keys[LIST_RESOURCES_CACHE_MAX], { value: "newest" });
  assert.equal(productionSizedCache.get(keys[1]), undefined);
  assert.equal(productionSizedCache.get(keys[0]) !== undefined, true);
  assert.equal(productionSizedCache.get(keys[LIST_RESOURCES_CACHE_MAX]) !== undefined, true);
  assert.equal(productionSizedCache.size(), LIST_RESOURCES_CACHE_MAX);
});
