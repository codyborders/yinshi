import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { datadogRum } from "@datadog/browser-rum";
import { reactPlugin } from "@datadog/browser-rum-react";
import App from "./App";
import { AuthProvider } from "./hooks/useAuth";
import "./index.css";

declare const __GIT_COMMIT_HASH__: string;

datadogRum.init({
  applicationId: "6ca07893-ea15-4577-88cb-ef72b856ad3e",
  clientToken: "pubbe7e2760d9e429d5cda2d2eb49a408be",
  site: "datadoghq.com",
  service: "yinshi",
  env: "prod",
  version: __GIT_COMMIT_HASH__,
  sessionSampleRate: 100,
  sessionReplaySampleRate: 100,
  trackResources: true,
  trackUserInteractions: true,
  trackLongTasks: true,
  plugins: [reactPlugin({ router: false })],
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
