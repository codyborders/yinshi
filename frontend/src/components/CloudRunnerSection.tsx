import { useEffect, useState } from "react";

import {
  api,
  type CloudRunner,
  type CloudRunnerRegistration,
  type CloudRunnerStatus,
  type RunnerStorageProfile,
} from "../api/client";

type RunnerOptionId = "hosted" | RunnerStorageProfile;

type RunnerSetupOption = {
  id: RunnerOptionId;
  title: string;
  status: "Supported" | "Experimental";
  description: string;
  details: string[];
  warning?: string;
  runnerBacked: boolean;
};

const RUNNER_STORAGE_PROFILES: RunnerStorageProfile[] = [
  "aws_ebs_s3_files",
  "archil_shared_files",
  "archil_all_posix",
];

const RUNNER_OPTIONS: RunnerSetupOption[] = [
  {
    id: "hosted",
    title: "Hosted Yinshi",
    status: "Supported",
    description: "Use Yinshi exactly as it runs today with no cloud setup or runner token.",
    details: [
      "No AWS or Archil account required.",
      "Yinshi's hosted deployment owns the runtime storage.",
    ],
    runnerBacked: false,
  },
  {
    id: "aws_ebs_s3_files",
    title: "AWS BYOC: EBS plus S3 Files",
    status: "Supported",
    description: "Run sessions on user-owned AWS compute with active storage in the AWS account.",
    details: [
      "Live SQLite stays on encrypted runner EBS.",
      "Repos, worktrees, Pi config, sessions, and artifacts use S3 Files or local POSIX storage.",
    ],
    runnerBacked: true,
  },
  {
    id: "archil_shared_files",
    title: "Archil shared-files mode",
    status: "Experimental",
    description: "Keep SQLite on runner EBS and place shared project files on Archil POSIX storage.",
    details: [
      "Requires a user Archil account or disk.",
      "Standard Archil uses Archil-managed active storage/cache even with a user-owned backing bucket.",
    ],
    warning: "Experimental: Yinshi has not certified full Git and Pi workloads on Archil yet.",
    runnerBacked: true,
  },
  {
    id: "archil_all_posix",
    title: "Archil all-POSIX mode",
    status: "Experimental",
    description: "Put live SQLite and shared project files on one Archil POSIX tree.",
    details: [
      "Requires a user Archil account or disk.",
      "Standard Archil uses Archil-managed active storage/cache for active data.",
    ],
    warning: "Strong warning: live SQLite on Archil needs Yinshi-specific WAL, crash-recovery, and concurrency stress testing before production use.",
    runnerBacked: true,
  },
];

const STORAGE_LABELS: Record<string, string> = {
  archil: "Archil POSIX",
  archil_all_posix: "Archil all-POSIX",
  archil_shared_files: "Archil shared files",
  aws_ebs_s3_files: "AWS EBS plus S3 Files",
  local_posix: "Local POSIX",
  runner_ebs: "Runner EBS",
  s3_files_mount: "S3 Files mount",
  s3_files_or_local_posix: "S3 Files or local POSIX",
};

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

function optionStatusClass(status: RunnerSetupOption["status"]): string {
  if (status === "Experimental") {
    return "border-yellow-500/40 bg-yellow-500/10 text-yellow-200";
  }
  return "border-green-500/40 bg-green-500/10 text-green-300";
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

function storageLabel(value: string): string {
  return STORAGE_LABELS[value] ?? value;
}

function isRunnerStorageProfile(value: unknown): value is RunnerStorageProfile {
  if (typeof value !== "string") {
    return false;
  }
  return RUNNER_STORAGE_PROFILES.includes(value as RunnerStorageProfile);
}

function optionForRunner(runner: CloudRunner | null): RunnerOptionId {
  if (!runner) {
    return "hosted";
  }
  if (runner.status === "revoked") {
    return "hosted";
  }

  const profile = runner.capabilities.storage_profile;
  if (isRunnerStorageProfile(profile)) {
    return profile;
  }
  return "aws_ebs_s3_files";
}

function optionById(id: RunnerOptionId): RunnerSetupOption {
  const option = RUNNER_OPTIONS.find((candidate) => candidate.id === id);
  if (!option) {
    throw new Error(`Unknown runner option: ${id}`);
  }
  return option;
}

function runnerProfileValue(optionId: RunnerOptionId): RunnerStorageProfile | null {
  if (optionId === "hosted") {
    return null;
  }
  return optionId;
}

function optionCardClass(isSelected: boolean): string {
  const baseClass =
    "block h-full cursor-pointer rounded-xl border bg-gray-900/70 p-4 transition focus-within:ring-2 focus-within:ring-blue-400";
  if (isSelected) {
    return `${baseClass} border-blue-400/70 shadow-sm shadow-blue-900/20`;
  }
  return `${baseClass} border-gray-800 hover:border-gray-600`;
}

function CloudRunnerOptionCard({
  option,
  selectedOption,
  onSelect,
}: {
  option: RunnerSetupOption;
  selectedOption: RunnerOptionId;
  onSelect: (optionId: RunnerOptionId) => void;
}) {
  const isSelected = option.id === selectedOption;

  return (
    <label className={optionCardClass(isSelected)}>
      <input
        type="radio"
        name="runner-storage-option"
        value={option.id}
        checked={isSelected}
        onChange={() => onSelect(option.id)}
        className="sr-only"
      />
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-gray-100">{option.title}</div>
          <div className="mt-1 text-xs text-gray-500">
            {option.runnerBacked ? "Runner-backed" : "No runner required"}
          </div>
        </div>
        <span className={`rounded-full border px-2 py-0.5 text-[11px] ${optionStatusClass(option.status)}`}>
          {option.status}
        </span>
      </div>
      <p className="mt-3 text-sm text-gray-300">{option.description}</p>
      <ul className="mt-3 space-y-1 text-xs text-gray-500">
        {option.details.map((detail) => (
          <li key={detail}>{detail}</li>
        ))}
      </ul>
      {option.warning ? (
        <p className="mt-3 rounded border border-yellow-600/30 bg-yellow-500/10 px-3 py-2 text-xs text-yellow-100">
          {option.warning}
        </p>
      ) : null}
    </label>
  );
}

export default function CloudRunnerSection() {
  const [runner, setRunner] = useState<CloudRunner | null>(null);
  const [registration, setRegistration] = useState<CloudRunnerRegistration | null>(null);
  const [selectedOption, setSelectedOption] = useState<RunnerOptionId>("hosted");
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
      setSelectedOption(optionForRunner(loadedRunner));
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
    const storageProfile = runnerProfileValue(selectedOption);
    if (!storageProfile) {
      setError("Hosted Yinshi does not need a runner registration token.");
      return;
    }

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
        storage_profile: storageProfile,
      });
      setRegistration(createdRegistration);
      setRunner(createdRegistration.runner);
      setSelectedOption(optionForRunner(createdRegistration.runner));
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
  const selectedRunnerProfile = runnerProfileValue(selectedOption);
  const selectedOptionDetails = optionById(selectedOption);

  return (
    <section className="mb-8 rounded-xl border border-gray-800 bg-gray-900/60 p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-200">Cloud Runner</h2>
          <p className="mt-2 max-w-3xl text-sm text-gray-400">
            Choose hosted Yinshi, supported AWS BYOC storage, or experimental Archil-backed
            POSIX storage. Archil options require the user to sign up for Archil and accept
            Archil-managed active storage/cache.
          </p>
        </div>
        <span className={`rounded-full border px-3 py-1 text-xs ${runnerStatusClass(status)}`}>
          {statusLabel}
        </span>
      </div>

      <fieldset className="mt-5">
        <legend className="text-sm font-medium text-gray-200">Storage option</legend>
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          {RUNNER_OPTIONS.map((option) => (
            <CloudRunnerOptionCard
              key={option.id}
              option={option}
              selectedOption={selectedOption}
              onSelect={setSelectedOption}
            />
          ))}
        </div>
      </fieldset>

      {runner ? (
        <div className="mt-5 grid gap-3 text-sm text-gray-400 md:grid-cols-3 lg:grid-cols-6">
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">Runner</div>
            <div className="mt-1 text-gray-200">{runner.name}</div>
          </div>
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">Region</div>
            <div className="mt-1 text-gray-200">{runner.region}</div>
          </div>
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500">Profile</div>
            <div className="mt-1 text-gray-200">
              {storageLabel(runnerCapability(runner, "storage_profile", "aws_ebs_s3_files"))}
            </div>
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

      {selectedRunnerProfile ? (
        <div className="mt-5 rounded-lg border border-gray-800 bg-gray-950/50 p-4">
          <div className="mb-3">
            <h3 className="text-sm font-semibold text-gray-100">
              Create a runner token for {selectedOptionDetails.title}
            </h3>
            <p className="mt-1 text-sm text-gray-400">
              The runner starts on AWS EC2. The selected storage profile controls validation,
              default paths, and advertised capabilities.
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-[1fr_180px_auto]">
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
        </div>
      ) : (
        <div className="mt-5 rounded-lg border border-gray-800 bg-gray-950/50 p-4 text-sm text-gray-400">
          Hosted Yinshi does not create a runner record or registration token. Select a runner-backed
          option only when you want user-owned AWS compute.
        </div>
      )}

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
            rows={8}
            className="mt-3 w-full rounded border border-blue-400/30 bg-gray-950 px-3 py-2 font-mono text-xs text-blue-50"
          />
          <p className="mt-2 text-xs text-blue-100/70">
            Use docs/deployment/aws-runner-cloudformation.yaml and
            docs/deployment/runner-storage-options.md to launch the EC2 runner.
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
