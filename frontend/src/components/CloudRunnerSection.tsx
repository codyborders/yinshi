import { useEffect, useState } from "react";

import {
  api,
  type CloudRunner,
  type CloudRunnerRegistration,
  type CloudRunnerStatus,
} from "../api/client";

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "Never used";
  }
  return new Date(value).toLocaleString();
}

function runnerStatusClass(status: CloudRunnerStatus): string {
  if (status === "online") {
    return "border-green-500/40 bg-green-500/10 text-green-300";
  }
  if (status === "pending") {
    return "border-yellow-500/40 bg-yellow-500/10 text-yellow-200";
  }
  if (status === "revoked") {
    return "border-red-500/40 bg-red-500/10 text-red-300";
  }
  return "border-gray-600 bg-gray-800 text-gray-300";
}

function runnerEnvironmentText(registration: CloudRunnerRegistration): string {
  return Object.entries(registration.environment)
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function runnerCapability(runner: CloudRunner, key: string, fallback: string): string {
  const value = runner.capabilities[key];
  return typeof value === "string" && value ? value : fallback;
}

const STORAGE_LABELS: Record<string, string> = {
  local_posix: "Local POSIX",
  runner_ebs: "Runner EBS",
  s3_files_mount: "S3 Files mount",
  s3_files_or_local_posix: "S3 Files or local POSIX",
};

function storageLabel(value: string): string {
  return STORAGE_LABELS[value] ?? value;
}

export default function CloudRunnerSection() {
  const [runner, setRunner] = useState<CloudRunner | null>(null);
  const [registration, setRegistration] = useState<CloudRunnerRegistration | null>(null);
  const [name, setName] = useState("AWS runner");
  const [region, setRegion] = useState("us-east-1");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadRunner() {
    setLoading(true);
    try {
      const loadedRunner = await api.get<CloudRunner | null>("/api/settings/runner");
      setRunner(loadedRunner);
      setError(null);
    } catch (loadError) {
      setError(errorMessage(loadError, "Failed to load cloud runner"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadRunner();
  }, []);

  async function createRunner() {
    const normalizedName = name.trim();
    const normalizedRegion = region.trim();
    if (!normalizedName || !normalizedRegion) {
      setError("Runner name and AWS region are required.");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const createdRegistration = await api.post<CloudRunnerRegistration>("/api/settings/runner", {
        name: normalizedName,
        cloud_provider: "aws",
        region: normalizedRegion,
      });
      setRegistration(createdRegistration);
      setRunner(createdRegistration.runner);
    } catch (createError) {
      setError(errorMessage(createError, "Failed to create cloud runner"));
    } finally {
      setSaving(false);
    }
  }

  async function revokeRunner() {
    if (!runner) {
      return;
    }
    setRevoking(true);
    setError(null);
    try {
      await api.delete("/api/settings/runner");
      setRegistration(null);
      await loadRunner();
    } catch (revokeError) {
      setError(errorMessage(revokeError, "Failed to revoke cloud runner"));
    } finally {
      setRevoking(false);
    }
  }

  const status = runner?.status ?? "offline";
  const createButtonLabel = runner && runner.status !== "revoked" ? "Replace Runner" : "Create Token";
  const statusLabel = loading ? "Loading" : runner?.status ?? "Not configured";

  return (
    <section className="mb-8 rounded-xl border border-gray-800 bg-gray-900/60 p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-200">Cloud Runner</h2>
          <p className="mt-2 max-w-3xl text-sm text-gray-400">
            Run Yinshi sessions on user-owned AWS compute. Live SQLite stays on
            runner EBS, while repos, worktrees, Pi config, sessions, and artifacts
            can live on an S3 Files mount.
          </p>
        </div>
        <span className={`rounded-full border px-3 py-1 text-xs ${runnerStatusClass(status)}`}>
          {statusLabel}
        </span>
      </div>

      {runner ? (
        <div className="mt-4 grid gap-3 text-sm text-gray-400 md:grid-cols-3 lg:grid-cols-5">
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">Runner</div>
            <div className="mt-1 text-gray-200">{runner.name}</div>
          </div>
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">Region</div>
            <div className="mt-1 text-gray-200">{runner.region}</div>
          </div>
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">Last heartbeat</div>
            <div className="mt-1 text-gray-200">{formatTimestamp(runner.last_heartbeat_at)}</div>
          </div>
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">SQLite</div>
            <div className="mt-1 text-gray-200">
              {storageLabel(runnerCapability(runner, "sqlite_storage", "Runner EBS"))}
            </div>
          </div>
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">Shared files</div>
            <div className="mt-1 text-gray-200">
              {storageLabel(runnerCapability(runner, "shared_files_storage", "S3 Files ready"))}
            </div>
          </div>
        </div>
      ) : null}

      <div className="mt-5 grid gap-3 md:grid-cols-[1fr_180px_auto]">
        <input
          type="text"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Runner name"
          className="rounded border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-500"
        />
        <input
          type="text"
          value={region}
          onChange={(event) => setRegion(event.target.value)}
          placeholder="AWS region"
          className="rounded border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-500"
        />
        <button
          type="button"
          onClick={() => {
            void createRunner();
          }}
          disabled={saving}
          className="btn-primary px-4 py-2 text-sm disabled:opacity-50"
        >
          {saving ? "Creating..." : createButtonLabel}
        </button>
      </div>

      {registration ? (
        <div className="mt-5 rounded-lg border border-blue-500/30 bg-blue-500/10 p-4">
          <h3 className="text-sm font-semibold text-blue-100">One-time registration values</h3>
          <p className="mt-1 text-sm text-blue-100/80">
            Store these in the AWS CloudFormation template or runner environment now.
            The token expires at {formatTimestamp(registration.registration_token_expires_at)}.
          </p>
          <textarea
            readOnly
            value={runnerEnvironmentText(registration)}
            rows={5}
            className="mt-3 w-full rounded border border-blue-400/30 bg-gray-950 px-3 py-2 font-mono text-xs text-blue-50"
          />
          <p className="mt-2 text-xs text-blue-100/70">
            Use docs/deployment/aws-runner-cloudformation.yaml to launch the EC2 runner.
          </p>
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-gray-500">
          Registration tokens are shown once. Runner bearer tokens are hashed before storage.
        </p>
        {runner && runner.status !== "revoked" ? (
          <button
            type="button"
            onClick={() => {
              void revokeRunner();
            }}
            disabled={revoking}
            className="text-sm text-red-400 hover:text-red-300 disabled:opacity-50"
          >
            {revoking ? "Revoking..." : "Revoke Runner"}
          </button>
        ) : null}
      </div>

      {error ? <p className="mt-3 text-sm text-red-400">{error}</p> : null}
    </section>
  );
}
