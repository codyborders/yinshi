import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { datadogRum } from "@datadog/browser-rum";
import App from "./App";
import { AuthProvider } from "./hooks/useAuth";
import { preloadEncryptedRunnerCrypto } from "./runner/encryptedRunnerClient";
import { createRumConfiguration } from "./rum";
import "./index.css";

declare const __GIT_COMMIT_HASH__: string;

if (
  window.location.pathname === "/app" ||
  window.location.pathname.startsWith("/app/")
) {
  void preloadEncryptedRunnerCrypto().catch(() => undefined);
}

/* Defer Datadog RUM so it does not compete with first paint. */
const initDatadogRum = () => {
  datadogRum.init(createRumConfiguration(__GIT_COMMIT_HASH__));
};

if ("requestIdleCallback" in window) {
  window.requestIdleCallback(initDatadogRum);
} else {
  setTimeout(initDatadogRum, 0);
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
