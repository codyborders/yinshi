/// <reference lib="webworker" />

import { HistoryCacheStore } from "./sessionHistoryCacheCore";
import { bindHistoryCachePort } from "./sessionHistoryCachePort";

const store = new HistoryCacheStore();
const workerScope = self as unknown as SharedWorkerGlobalScope;

workerScope.onconnect = (event: MessageEvent) => {
  const port = event.ports[0];
  if (!port) return;
  void bindHistoryCachePort(port, store).catch(() => port.close());
};
