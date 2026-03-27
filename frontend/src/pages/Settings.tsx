import { useEffect, useMemo, useState } from "react";

import {
  api,
  pollAuthFlow,
  type ProviderConnection,
  type ProviderDescriptor,
} from "../api/client";
import PiConfigSection from "../components/PiConfigSection";
import { useAuth } from "../hooks/useAuth";
import { useCatalog } from "../hooks/useCatalog";

function formatTimestamp(timestamp: string | null): string {
  if (!timestamp) {
    return "Never used";
  }
  return new Date(timestamp).toLocaleString();
}

function buildInitialConfig(provider: ProviderDescriptor): Record<string, string> {
  const initialConfig: Record<string, string> = {};
  for (const field of provider.setup_fields) {
    initialConfig[field.key] = "";
  }
  return initialConfig;
}

function ProviderCard({
  provider,
  connection,
  onConnectionChange,
}: {
  provider: ProviderDescriptor;
  connection: ProviderConnection | undefined;
  onConnectionChange: () => Promise<void>;
}) {
  const [authStrategy, setAuthStrategy] = useState(provider.auth_strategies[0] || "api_key");
  const [secret, setSecret] = useState("");
  const [label, setLabel] = useState("");
  const [config, setConfig] = useState<Record<string, string>>(() => buildInitialConfig(provider));
  const [saving, setSaving] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setAuthStrategy(provider.auth_strategies[0] || "api_key");
    setConfig(buildInitialConfig(provider));
    setSecret("");
    setLabel("");
    setError(null);
  }, [provider]);

  const hasKeyForm = authStrategy === "api_key" || authStrategy === "api_key_with_config";
  const hasOauth = authStrategy === "oauth";

  const nonSecretConfig = useMemo(() => {
    const trimmedConfigEntries = Object.entries(config)
      .map(([key, value]) => [key, value.trim()] as const)
      .filter(([, value]) => value.length > 0);
    return Object.fromEntries(trimmedConfigEntries);
  }, [config]);

  async function saveConnection() {
    if (!hasKeyForm || !secret.trim()) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.post("/api/settings/connections", {
        provider: provider.id,
        auth_strategy: authStrategy,
        secret,
        label,
        config: nonSecretConfig,
      });
      setSecret("");
      setLabel("");
      setConfig(buildInitialConfig(provider));
      await onConnectionChange();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save provider connection");
    } finally {
      setSaving(false);
    }
  }

  async function connectProvider() {
    if (!hasOauth) {
      return;
    }
    setConnecting(true);
    setError(null);
    try {
      const started = await api.post<{
        flow_id: string;
        provider: string;
        auth_url: string;
        instructions: string | null;
      }>(`/auth/providers/${provider.id}/start`);
      if (started.auth_url) {
        window.open(started.auth_url, "_blank", "noopener,noreferrer");
      }
      for (let attempt = 0; attempt < 120; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const status = await pollAuthFlow(provider.id, started.flow_id);
        if (status.status === "complete") {
          await onConnectionChange();
          return;
        }
        if (status.status === "error") {
          throw new Error("Provider authorization failed");
        }
      }
      throw new Error("Provider authorization timed out");
    } catch (connectError) {
      setError(connectError instanceof Error ? connectError.message : "Provider authorization failed");
    } finally {
      setConnecting(false);
    }
  }

  async function removeConnection() {
    if (!connection) {
      return;
    }
    try {
      await api.delete(`/api/settings/connections/${connection.id}`);
      await onConnectionChange();
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : "Failed to remove provider connection");
    }
  }

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900/70 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-gray-100">{provider.label}</h3>
          <p className="mt-1 text-sm text-gray-400">
            {provider.model_count} models
          </p>
        </div>
        {connection ? (
          <div className="text-right text-xs text-gray-500">
            <div className="text-green-400">Connected</div>
            {connection.label ? (
              <div className="text-gray-300">{connection.label}</div>
            ) : null}
            <div>{formatTimestamp(connection.last_used_at)}</div>
          </div>
        ) : (
          <div className="text-xs text-gray-500">Not connected</div>
        )}
      </div>

      {provider.auth_strategies.length > 1 && (
        <div className="mt-4 flex gap-2">
          {provider.auth_strategies.map((strategy) => (
            <button
              key={strategy}
              type="button"
              onClick={() => setAuthStrategy(strategy)}
              className={`rounded px-3 py-1 text-xs ${
                authStrategy === strategy
                  ? "bg-gray-200 text-gray-900"
                  : "border border-gray-700 text-gray-300"
              }`}
            >
              {strategy === "oauth" ? "Connect" : strategy === "api_key_with_config" ? "Key + Config" : "API Key"}
            </button>
          ))}
        </div>
      )}

      {hasKeyForm && (
        <div className="mt-4 space-y-3">
          <input
            type="text"
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder="Label (optional)"
            className="w-full rounded border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-500"
          />
          <input
            type="password"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            placeholder="Enter provider secret"
            className="w-full rounded border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-500"
          />
          {provider.setup_fields.map((field) => (
            <input
              key={field.key}
              type={field.secret ? "password" : "text"}
              value={config[field.key] || ""}
              onChange={(event) => {
                setConfig((previousConfig) => ({
                  ...previousConfig,
                  [field.key]: event.target.value,
                }));
              }}
              placeholder={field.label}
              className="w-full rounded border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-500"
            />
          ))}
          <button
            type="button"
            onClick={() => {
              void saveConnection();
            }}
            disabled={saving || !secret.trim()}
            className="btn-primary px-4 py-2 text-sm disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save Connection"}
          </button>
        </div>
      )}

      {hasOauth && (
        <div className="mt-4 space-y-3">
          <p className="text-sm text-gray-400">
            Open the provider authorization flow in a new window, complete sign-in,
            and this page will pick up the connected account automatically.
          </p>
          <button
            type="button"
            onClick={() => {
              void connectProvider();
            }}
            disabled={connecting}
            className="btn-primary px-4 py-2 text-sm disabled:opacity-50"
          >
            {connecting ? "Connecting..." : "Connect Provider"}
          </button>
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <a
          href={provider.docs_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-blue-400 hover:text-blue-300"
        >
          Provider docs
        </a>
        {connection && (
          <button
            type="button"
            onClick={() => {
              void removeConnection();
            }}
            className="text-sm text-red-400 hover:text-red-300"
          >
            Remove
          </button>
        )}
      </div>

      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
    </div>
  );
}

export default function Settings() {
  const { email } = useAuth();
  const { catalog, loading, error: catalogError } = useCatalog();
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [loadingConnections, setLoadingConnections] = useState(true);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);

  async function loadConnections() {
    setLoadingConnections(true);
    try {
      const loadedConnections = await api.get<ProviderConnection[]>("/api/settings/connections");
      setConnections(loadedConnections);
      setConnectionsError(null);
    } catch (loadError) {
      setConnectionsError(loadError instanceof Error ? loadError.message : "Failed to load connections");
    } finally {
      setLoadingConnections(false);
    }
  }

  useEffect(() => {
    void loadConnections();
  }, []);

  const connectionByProviderId = useMemo(() => {
    const mappedConnections = new Map<string, ProviderConnection>();
    for (const connection of connections) {
      if (!mappedConnections.has(connection.provider)) {
        mappedConnections.set(connection.provider, connection);
      }
    }
    return mappedConnections;
  }, [connections]);

  return (
    <div className="mx-auto max-w-5xl p-6">
      <h1 className="mb-6 text-2xl font-bold text-gray-100">Settings</h1>

      <section className="mb-8">
        <h2 className="mb-2 text-lg font-semibold text-gray-200">Account</h2>
        <p className="text-sm text-gray-400">{email}</p>
      </section>

      <section>
        <h2 className="mb-4 text-lg font-semibold text-gray-200">Providers</h2>
        <p className="mb-4 text-sm text-gray-400">
          Yinshi does not provide shared model credits. Connect your own model
          providers here before starting sessions. Secrets are encrypted at rest
          and never shown again after saving.
        </p>

        {(loading || loadingConnections) && (
          <div className="rounded border border-gray-700 bg-gray-800 px-4 py-3 text-sm text-gray-400">
            Loading provider catalog...
          </div>
        )}

        {(catalogError || connectionsError) && (
          <div className="mb-4 rounded border border-red-900/50 bg-gray-800 px-4 py-3 text-sm text-red-400">
            {catalogError || connectionsError}
          </div>
        )}

        {catalog && (
          <div className="grid gap-4 md:grid-cols-2">
            {catalog.providers.map((provider) => (
              <ProviderCard
                key={provider.id}
                provider={provider}
                connection={connectionByProviderId.get(provider.id)}
                onConnectionChange={loadConnections}
              />
            ))}
          </div>
        )}
      </section>

      <PiConfigSection />
    </div>
  );
}
