import { useEffect, useMemo, useState } from "react";

import {
  api,
  type CloudRunner,
  type ProviderAuthStart,
  type ProviderAuthStatus,
  type ProviderConnection,
  type ProviderDescriptor,
} from "../api/client";
import CloudRunnerSection from "../components/CloudRunnerSection";
import PiConfigSection from "../components/PiConfigSection";
import PiReleaseNotesSection from "../components/PiReleaseNotesSection";
import { useAuth } from "../hooks/useAuth";
import { useCatalog } from "../hooks/useCatalog";
import {
  provisionRuntimeRef,
  resolveRuntimeRef,
} from "../runtime/resolveRuntime";
import type { UnresolvedRuntimeRef } from "../runtime/runtimeRef";
import {
  createRuntimeTransport,
  type RuntimeRef,
  type RuntimeTransport,
} from "../runtime/runtimeTransport";

function formatTimestamp(timestamp: string | null): string {
  if (!timestamp) {
    return "Never used";
  }
  return new Date(timestamp).toLocaleString();
}

function buildInitialConfig(
  provider: ProviderDescriptor,
): Record<string, string> {
  const initialConfig: Record<string, string> = {};
  for (const field of provider.setup_fields) {
    initialConfig[field.key] = "";
  }
  return initialConfig;
}

function normalizeFieldValue(value: string | undefined): string {
  return (value || "").trim();
}

function providerAuthorizationUrl(value: string): string {
  let authorizationUrl: URL;
  try {
    authorizationUrl = new URL(value);
  } catch {
    throw new Error("Provider authorization URL is invalid");
  }
  if (
    authorizationUrl.protocol !== "https:" ||
    authorizationUrl.username !== "" ||
    authorizationUrl.password !== ""
  ) {
    throw new Error(
      "Provider authorization URL must use credential-free HTTPS",
    );
  }
  return authorizationUrl.href;
}

function defaultOauthInstructions(): string {
  return "Open the provider authorization flow in a new window, complete sign-in, and Yinshi will finish the connection automatically. If the provider redirects to localhost and the browser shows an error, copy the full URL from the address bar and paste it here.";
}

function ProviderCard({
  provider,
  connection,
  onConnectionChange,
  transport,
}: {
  provider: ProviderDescriptor;
  connection: ProviderConnection | undefined;
  onConnectionChange: () => Promise<void>;
  transport: RuntimeTransport;
}) {
  const [authStrategy, setAuthStrategy] = useState(
    provider.auth_strategies[0] || "api_key",
  );
  const [secret, setSecret] = useState("");
  const [label, setLabel] = useState("");
  const [config, setConfig] = useState<Record<string, string>>(() =>
    buildInitialConfig(provider),
  );
  const [saving, setSaving] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [oauthFlowId, setOauthFlowId] = useState<string | null>(null);
  const [oauthInstructions, setOauthInstructions] = useState<string | null>(
    null,
  );
  const [oauthProgress, setOauthProgress] = useState<string[]>([]);
  const [oauthManualInputRequired, setOauthManualInputRequired] =
    useState(false);
  const [oauthManualInputPrompt, setOauthManualInputPrompt] = useState<
    string | null
  >(null);
  const [oauthManualInputSubmitted, setOauthManualInputSubmitted] =
    useState(false);
  const [oauthManualInputValue, setOauthManualInputValue] = useState("");
  const [submittingOauthInput, setSubmittingOauthInput] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function resetOauthFlowState() {
    setOauthFlowId(null);
    setOauthInstructions(null);
    setOauthProgress([]);
    setOauthManualInputRequired(false);
    setOauthManualInputPrompt(null);
    setOauthManualInputSubmitted(false);
    setOauthManualInputValue("");
    setSubmittingOauthInput(false);
  }

  function applyOauthFlowState(flow: ProviderAuthStart | ProviderAuthStatus) {
    setOauthFlowId(flow.flow_id);
    if ("instructions" in flow) {
      setOauthInstructions(flow.instructions ?? null);
    }
    if ("progress" in flow && Array.isArray(flow.progress)) {
      setOauthProgress(flow.progress);
    }
    if ("manual_input_required" in flow) {
      setOauthManualInputRequired(Boolean(flow.manual_input_required));
    }
    if ("manual_input_prompt" in flow) {
      setOauthManualInputPrompt(flow.manual_input_prompt ?? null);
    }
    if ("manual_input_submitted" in flow) {
      setOauthManualInputSubmitted(Boolean(flow.manual_input_submitted));
    }
  }

  useEffect(() => {
    setAuthStrategy(provider.auth_strategies[0] || "api_key");
    setConfig(buildInitialConfig(provider));
    setSecret("");
    setLabel("");
    resetOauthFlowState();
    setError(null);
  }, [provider]);

  const hasKeyForm =
    authStrategy === "api_key" || authStrategy === "api_key_with_config";
  const hasOauth = authStrategy === "oauth";
  const secretSetupFields = useMemo(
    () => provider.setup_fields.filter((field) => field.secret),
    [provider.setup_fields],
  );
  const publicSetupFields = useMemo(
    () => provider.setup_fields.filter((field) => !field.secret),
    [provider.setup_fields],
  );
  const nonSecretConfig = useMemo(() => {
    const trimmedConfigEntries = publicSetupFields
      .map(
        (field) => [field.key, normalizeFieldValue(config[field.key])] as const,
      )
      .filter(([, value]) => value.length > 0);
    return Object.fromEntries(trimmedConfigEntries);
  }, [config, publicSetupFields]);
  const structuredSecret = useMemo(() => {
    const normalizedSecret = normalizeFieldValue(secret);
    if (authStrategy !== "api_key_with_config") {
      return normalizedSecret;
    }
    const secretPayload: Record<string, string> = { apiKey: normalizedSecret };
    for (const field of secretSetupFields) {
      const fieldValue = normalizeFieldValue(config[field.key]);
      if (fieldValue) {
        secretPayload[field.key] = fieldValue;
      }
    }
    return secretPayload;
  }, [authStrategy, config, secret, secretSetupFields]);
  const missingRequiredField = useMemo(() => {
    if (!hasKeyForm) {
      return null;
    }
    if (!normalizeFieldValue(secret)) {
      return "API key";
    }
    for (const field of provider.setup_fields) {
      if (!field.required) {
        continue;
      }
      if (!normalizeFieldValue(config[field.key])) {
        return field.label;
      }
    }
    return null;
  }, [config, hasKeyForm, provider.setup_fields, secret]);

  async function saveConnection() {
    if (!hasKeyForm || missingRequiredField) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await transport.post("/api/settings/connections", {
        provider: provider.id,
        auth_strategy: authStrategy,
        secret: structuredSecret,
        label,
        config: nonSecretConfig,
      });
      setSecret("");
      setLabel("");
      setConfig(buildInitialConfig(provider));
      await onConnectionChange();
    } catch (saveError) {
      setError(
        saveError instanceof Error
          ? saveError.message
          : "Failed to save provider connection",
      );
    } finally {
      setSaving(false);
    }
  }

  async function connectProvider() {
    if (!hasOauth) {
      return;
    }
    setConnecting(true);
    resetOauthFlowState();
    setError(null);
    try {
      const started = await transport.post<ProviderAuthStart>(
        `/auth/providers/${provider.id}/start`,
      );
      applyOauthFlowState(started);
      if (started.auth_url) {
        window.open(
          providerAuthorizationUrl(started.auth_url),
          "_blank",
          "noopener,noreferrer",
        );
      }
      for (let attempt = 0; attempt < 600; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const status = await transport.get<ProviderAuthStatus>(
          `/auth/providers/${provider.id}/callback?flow_id=${encodeURIComponent(started.flow_id)}`,
        );
        applyOauthFlowState(status);
        if (status.status === "complete") {
          resetOauthFlowState();
          await onConnectionChange();
          return;
        }
        if (status.status === "error") {
          throw new Error(status.error || "Provider authorization failed");
        }
      }
      throw new Error("Provider authorization timed out");
    } catch (connectError) {
      setError(
        connectError instanceof Error
          ? connectError.message
          : "Provider authorization failed",
      );
    } finally {
      setConnecting(false);
    }
  }

  async function submitOauthCallbackInput() {
    if (!oauthFlowId) {
      setError("Provider authorization flow is not active");
      return;
    }
    const normalizedAuthorizationInput = normalizeFieldValue(
      oauthManualInputValue,
    );
    if (!normalizedAuthorizationInput) {
      setError("Authorization input is required");
      return;
    }
    setSubmittingOauthInput(true);
    setError(null);
    try {
      const status = await transport.post<ProviderAuthStatus>(
        `/auth/providers/${provider.id}/callback`,
        {
          flow_id: oauthFlowId,
          authorization_input: normalizedAuthorizationInput,
        },
      );
      applyOauthFlowState(status);
      setOauthManualInputValue("");
    } catch (submitError) {
      setError(
        submitError instanceof Error
          ? submitError.message
          : "Failed to submit authorization input",
      );
    } finally {
      setSubmittingOauthInput(false);
    }
  }

  async function removeConnection() {
    if (!connection) {
      return;
    }
    try {
      await transport.delete(`/api/settings/connections/${connection.id}`);
      await onConnectionChange();
    } catch (deleteError) {
      setError(
        deleteError instanceof Error
          ? deleteError.message
          : "Failed to remove provider connection",
      );
    }
  }

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900/70 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-gray-100">
            {provider.label}
          </h3>
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
              {strategy === "oauth"
                ? "Connect"
                : strategy === "api_key_with_config"
                  ? "Key + Config"
                  : "API Key"}
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
            placeholder="Enter API key"
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
            disabled={saving || missingRequiredField !== null}
            className="btn-primary px-4 py-2 text-sm disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save Connection"}
          </button>
          {missingRequiredField ? (
            <p className="text-sm text-gray-500">
              {missingRequiredField} is required.
            </p>
          ) : null}
        </div>
      )}

      {hasOauth && (
        <div className="mt-4 space-y-3">
          <p className="text-sm text-gray-400">
            {oauthInstructions || defaultOauthInstructions()}
          </p>
          {oauthProgress.length > 0 ? (
            <p className="text-xs text-gray-500">
              {oauthProgress[oauthProgress.length - 1]}
            </p>
          ) : null}
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
          {oauthManualInputRequired && !connection ? (
            <div className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
              <p className="text-sm text-gray-300">
                {oauthManualInputPrompt ||
                  "Paste the final redirect URL or authorization code here."}
              </p>
              <textarea
                value={oauthManualInputValue}
                onChange={(event) =>
                  setOauthManualInputValue(event.target.value)
                }
                placeholder="http://localhost:1455/auth/callback?code=..."
                rows={3}
                className="mt-3 w-full rounded border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-500"
              />
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  onClick={() => {
                    void submitOauthCallbackInput();
                  }}
                  disabled={submittingOauthInput || oauthManualInputSubmitted}
                  className="rounded border border-gray-600 px-4 py-2 text-sm text-gray-100 disabled:opacity-50"
                >
                  {submittingOauthInput
                    ? "Submitting..."
                    : oauthManualInputSubmitted
                      ? "Submitted"
                      : "Submit Callback URL"}
                </button>
                {oauthManualInputSubmitted ? (
                  <span className="text-xs text-gray-500">
                    Waiting for the provider to finish the OAuth flow.
                  </span>
                ) : null}
              </div>
            </div>
          ) : null}
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

type SettingsTab =
  "providers" | "cloud-runner" | "pi-config" | "pi-release-notes";

const SETTINGS_TABS: Array<{ id: SettingsTab; label: string }> = [
  { id: "providers", label: "Providers" },
  { id: "cloud-runner", label: "Cloud runner" },
  { id: "pi-config", label: "Pi config" },
  { id: "pi-release-notes", label: "Pi release notes" },
];

function ProvidersSection({ transport }: { transport: RuntimeTransport }) {
  const { catalog, loading, error: catalogError } = useCatalog(transport);
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [loadingConnections, setLoadingConnections] = useState(true);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);

  async function loadConnections() {
    setLoadingConnections(true);
    try {
      const loadedConnections = await transport.get<ProviderConnection[]>(
        "/api/settings/connections",
      );
      setConnections(loadedConnections);
      setConnectionsError(null);
    } catch (loadError) {
      setConnectionsError(
        loadError instanceof Error
          ? loadError.message
          : "Failed to load connections",
      );
    } finally {
      setLoadingConnections(false);
    }
  }

  useEffect(() => {
    void loadConnections();
  }, [transport]);

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
    <section aria-labelledby="providers-settings-heading">
      <h2
        id="providers-settings-heading"
        className="mb-4 text-lg font-semibold text-gray-200"
      >
        Providers
      </h2>
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
              key={`${transport.runtime.location}-${provider.id}`}
              provider={provider}
              connection={connectionByProviderId.get(provider.id)}
              onConnectionChange={loadConnections}
              transport={transport}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function RuntimeLocationSelector({
  runtime,
  primaryRuntime,
  byocRuntime,
  onSelect,
}: {
  runtime: RuntimeRef;
  primaryRuntime: RuntimeRef;
  byocRuntime: RuntimeRef | null;
  onSelect: (runtime: RuntimeRef) => void;
}) {
  const selectedByoc = runtime.location === "byoc";
  const selectedPrimary = runtime.location === primaryRuntime.location;
  return (
    <section className="mb-6" aria-labelledby="runtime-location-heading">
      <h2
        id="runtime-location-heading"
        className="mb-2 text-sm font-semibold text-gray-300"
      >
        Provider location
      </h2>
      <p className="mb-3 text-sm text-gray-500">
        Credentials stay in the selected execution location and are never copied
        automatically.
      </p>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => onSelect(primaryRuntime)}
          className={`rounded border px-3 py-2 text-sm ${
            selectedPrimary
              ? "border-gray-500 bg-gray-800 text-gray-100"
              : "border-gray-800 text-gray-400 hover:border-gray-600"
          }`}
        >
          {window.yinshiDesktop ? "This Mac" : "Hosted Yinshi"}
        </button>
        {window.yinshiDesktop ? (
          <button
            type="button"
            onClick={() => onSelect({ location: "hosted" })}
            className={`rounded border px-3 py-2 text-sm ${
              runtime.location === "hosted"
                ? "border-gray-500 bg-gray-800 text-gray-100"
                : "border-gray-800 text-gray-400 hover:border-gray-600"
            }`}
          >
            Hosted Yinshi
          </button>
        ) : null}
        {byocRuntime ? (
          <button
            type="button"
            onClick={() => onSelect(byocRuntime)}
            className={`rounded border px-3 py-2 text-sm ${
              selectedByoc
                ? "border-gray-500 bg-gray-800 text-gray-100"
                : "border-gray-800 text-gray-400 hover:border-gray-600"
            }`}
          >
            BYOC runner
          </button>
        ) : null}
      </div>
    </section>
  );
}

function SettingsTabButton({
  tab,
  activeTab,
  onSelect,
}: {
  tab: { id: SettingsTab; label: string };
  activeTab: SettingsTab;
  onSelect: (tab: SettingsTab) => void;
}) {
  const selected = activeTab === tab.id;
  return (
    <button
      id={`settings-tab-${tab.id}`}
      type="button"
      role="tab"
      aria-selected={selected}
      aria-controls={`settings-panel-${tab.id}`}
      onClick={() => onSelect(tab.id)}
      className={`rounded-lg px-3 py-2 text-sm transition-colors ${
        selected
          ? "bg-gray-200 text-gray-950"
          : "border border-gray-800 text-gray-300 hover:bg-gray-800"
      }`}
    >
      {tab.label}
    </button>
  );
}

export default function Settings() {
  const { email } = useAuth();
  const requestedPrimaryRuntime = useMemo<UnresolvedRuntimeRef>(
    () =>
      window.yinshiDesktop ? { location: "local" } : { location: "hosted" },
    [],
  );
  const [activeTab, setActiveTab] = useState<SettingsTab>("providers");
  const [primaryRuntime, setPrimaryRuntime] = useState<RuntimeRef | null>(null);
  const [selectedRuntime, setSelectedRuntime] = useState<RuntimeRef | null>(null);
  const [runtimeResolutionError, setRuntimeResolutionError] = useState<
    string | null
  >(null);
  const [byocRuntime, setByocRuntime] = useState<RuntimeRef | null>(null);
  const [fileVaultStatus, setFileVaultStatus] = useState<
    "disabled" | "enabled" | "unknown" | null
  >(null);
  const [desktopProfiles, setDesktopProfiles] = useState<
    readonly {
      readonly user: { readonly id: string; readonly email: string };
      readonly hasCredentials: boolean;
      readonly active: boolean;
    }[]
  >([]);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [runtimeProvisioning, setRuntimeProvisioning] = useState(false);
  const runtimeTransport = useMemo(
    () =>
      selectedRuntime ? createRuntimeTransport(selectedRuntime) : null,
    [selectedRuntime],
  );

  useEffect(
    () => () => {
      runtimeTransport?.close();
    },
    [runtimeTransport],
  );

  async function provisionManagedRuntime() {
    if (runtimeProvisioning) return;
    setRuntimeProvisioning(true);
    setRuntimeResolutionError(null);
    try {
      const resolvedRuntime = await provisionRuntimeRef(requestedPrimaryRuntime);
      setPrimaryRuntime(resolvedRuntime);
      setSelectedRuntime(resolvedRuntime);
    } catch (provisionError) {
      setRuntimeResolutionError(
        provisionError instanceof Error
          ? provisionError.message
          : "Managed runtime setup failed",
      );
    } finally {
      setRuntimeProvisioning(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    setRuntimeResolutionError(null);
    void resolveRuntimeRef(requestedPrimaryRuntime)
      .then((resolvedRuntime) => {
        if (cancelled) return;
        setPrimaryRuntime(resolvedRuntime);
        setSelectedRuntime(resolvedRuntime);
      })
      .catch((resolutionError) => {
        if (cancelled) return;
        setRuntimeResolutionError(
          resolutionError instanceof Error
            ? resolutionError.message
            : "Failed to resolve runtime",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [requestedPrimaryRuntime]);

  useEffect(() => {
    const desktopBridge = window.yinshiDesktop;
    if (!desktopBridge) return;
    let cancelled = false;
    void desktopBridge
      .listProfiles()
      .then((profiles) => {
        if (!cancelled) setDesktopProfiles(profiles);
      })
      .catch(() => {
        if (!cancelled) setProfileError("Local profiles could not be loaded.");
      });
    void desktopBridge
      .fileVaultStatus()
      .then((result) => {
        if (!cancelled) setFileVaultStatus(result.status);
      })
      .catch(() => {
        if (!cancelled) setFileVaultStatus("unknown");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void api
      .get<CloudRunner | null>("/api/settings/runner")
      .then((runner) => {
        if (cancelled) return;
        if (
          runner?.noise_key_confirmed &&
          runner.noise_public_key &&
          runner.status !== "revoked"
        ) {
          setByocRuntime({
            location: "byoc",
            runnerId: runner.id,
            runnerPublicKey: runner.noise_public_key,
          });
        } else {
          setByocRuntime(null);
          setSelectedRuntime((currentRuntime) =>
            currentRuntime?.location === "byoc"
              ? primaryRuntime
              : currentRuntime,
          );
        }
      })
      .catch(() => {
        if (!cancelled) setByocRuntime(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeTab, primaryRuntime]);

  async function switchDesktopProfile(userId: string) {
    const desktopBridge = window.yinshiDesktop;
    if (!desktopBridge) return;
    setProfileError(null);
    try {
      await desktopBridge.switchProfile(userId);
    } catch {
      setProfileError(
        "Profile switch failed. Sign in to refresh this profile.",
      );
    }
  }

  async function removeDesktopProfile(userId: string, emailAddress: string) {
    const desktopBridge = window.yinshiDesktop;
    if (!desktopBridge) return;
    const confirmed = window.confirm(
      `Remove local profile ${emailAddress} and all of its local workspaces?`,
    );
    if (!confirmed) return;
    setProfileError(null);
    try {
      await desktopBridge.removeProfile(userId);
      setDesktopProfiles((profiles) =>
        profiles.filter((profile) => profile.user.id !== userId),
      );
    } catch {
      setProfileError("Local profile removal failed.");
    }
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl p-6 pb-12 pt-12 md:pt-6">
        <h1 className="mb-6 text-2xl font-bold text-gray-100">Settings</h1>

        <section className="mb-8">
          <h2 className="mb-2 text-lg font-semibold text-gray-200">Account</h2>
          <p className="text-sm text-gray-400">{email}</p>
        </section>

        {window.yinshiDesktop ? (
          <section className="mb-6" aria-labelledby="local-profiles-heading">
            <h2
              id="local-profiles-heading"
              className="mb-2 text-sm font-semibold text-gray-300"
            >
              Local profiles
            </h2>
            <div className="space-y-2">
              {desktopProfiles.map((profile) => (
                <div
                  key={profile.user.id}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-gray-800 bg-gray-900/70 px-4 py-3"
                >
                  <div>
                    <p className="text-sm font-medium text-gray-200">
                      {profile.user.email}
                    </p>
                    <p className="mt-1 text-xs text-gray-500">
                      {profile.active
                        ? "Active on this Mac"
                        : profile.hasCredentials
                          ? "Ready to switch"
                          : "Signed out; local data retained"}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    {!profile.active ? (
                      <button
                        type="button"
                        disabled={!profile.hasCredentials}
                        onClick={() =>
                          void switchDesktopProfile(profile.user.id)
                        }
                        className="rounded border border-gray-700 px-3 py-1.5 text-xs text-gray-300 hover:border-gray-500 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        Switch
                      </button>
                    ) : null}
                    <button
                      type="button"
                      onClick={() =>
                        void removeDesktopProfile(
                          profile.user.id,
                          profile.user.email,
                        )
                      }
                      className="rounded border border-red-900/60 px-3 py-1.5 text-xs text-red-300 hover:border-red-700"
                    >
                      Remove local data
                    </button>
                  </div>
                </div>
              ))}
              {desktopProfiles.length === 0 ? (
                <p className="text-sm text-gray-500">
                  No retained local profiles.
                </p>
              ) : null}
              {profileError ? (
                <p className="text-sm text-red-400">{profileError}</p>
              ) : null}
            </div>
          </section>
        ) : null}

        {window.yinshiDesktop ? (
          <section
            className="mb-6"
            aria-labelledby="local-storage-security-heading"
          >
            <h2
              id="local-storage-security-heading"
              className="mb-2 text-sm font-semibold text-gray-300"
            >
              Local storage security
            </h2>
            <div className="rounded-xl border border-gray-800 bg-gray-900/70 px-4 py-3">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <p className="text-sm font-medium text-gray-200">FileVault</p>
                  <p className="mt-1 text-xs text-gray-500">
                    Yinshi encrypts databases and secrets. FileVault also
                    protects repository files at rest.
                  </p>
                </div>
                <span
                  className={`text-xs font-medium ${
                    fileVaultStatus === "enabled"
                      ? "text-green-400"
                      : fileVaultStatus === "disabled"
                        ? "text-amber-300"
                        : "text-gray-500"
                  }`}
                >
                  {fileVaultStatus === "enabled"
                    ? "Enabled"
                    : fileVaultStatus === "disabled"
                      ? "Disabled"
                      : "Status unavailable"}
                </span>
              </div>
            </div>
          </section>
        ) : null}

        <div
          className="mb-6 flex flex-wrap gap-2"
          role="tablist"
          aria-label="Settings sections"
        >
          {SETTINGS_TABS.map((tab) => (
            <SettingsTabButton
              key={tab.id}
              tab={tab}
              activeTab={activeTab}
              onSelect={setActiveTab}
            />
          ))}
        </div>

        <div
          id={`settings-panel-${activeTab}`}
          role="tabpanel"
          aria-labelledby={`settings-tab-${activeTab}`}
        >
          {(activeTab === "providers" ||
            activeTab === "pi-config" ||
            activeTab === "pi-release-notes") &&
          primaryRuntime &&
          selectedRuntime ? (
            <RuntimeLocationSelector
              runtime={selectedRuntime}
              primaryRuntime={primaryRuntime}
              byocRuntime={byocRuntime}
              onSelect={setSelectedRuntime}
            />
          ) : null}
          {(activeTab === "providers" ||
            activeTab === "pi-config" ||
            activeTab === "pi-release-notes") &&
          !runtimeTransport &&
          !runtimeResolutionError ? (
            <div className="rounded border border-gray-700 bg-gray-800 px-4 py-3 text-sm text-gray-400">
              Loading provider catalog...
            </div>
          ) : null}
          {runtimeResolutionError ? (
            <div className="rounded border border-red-900/50 bg-gray-800 px-4 py-3 text-sm text-red-400">
              <p>{runtimeResolutionError}</p>
              {requestedPrimaryRuntime.location === "hosted" &&
              window.yinshiDesktop === undefined ? (
                <button
                  type="button"
                  onClick={() => void provisionManagedRuntime()}
                  disabled={runtimeProvisioning}
                  className="mt-3 rounded border border-gray-600 px-3 py-1.5 text-sm text-gray-200 hover:border-gray-500 disabled:opacity-50"
                >
                  {runtimeProvisioning
                    ? "Provisioning managed runtime..."
                    : "Provision managed runtime"}
                </button>
              ) : null}
            </div>
          ) : null}
          {activeTab === "providers" && runtimeTransport ? (
            <ProvidersSection transport={runtimeTransport} />
          ) : null}
          {activeTab === "cloud-runner" ? <CloudRunnerSection /> : null}
          {activeTab === "pi-config" && runtimeTransport ? (
            <PiConfigSection transport={runtimeTransport} />
          ) : null}
          {activeTab === "pi-release-notes" && runtimeTransport ? (
            <PiReleaseNotesSection transport={runtimeTransport} />
          ) : null}
        </div>
      </div>
    </div>
  );
}
