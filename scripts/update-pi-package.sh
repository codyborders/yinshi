#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--systemd-service" ]]; then
  app_root="${YINSHI_APP_ROOT:-/opt/yinshi}"
  service_user="${YINSHI_SERVICE_USER:-user}"
  status_path="${YINSHI_PI_UPDATE_STATUS_PATH:-$app_root/.runtime/pi-package-update.json}"
  runuser_bin="$(command -v runuser || true)"

  if [[ "$(id -u)" -ne 0 ]]; then
    echo "--systemd-service must run as root" >&2
    exit 1
  fi
  if [[ ! -x "$runuser_bin" ]]; then
    echo "runuser is required for the systemd service wrapper" >&2
    exit 1
  fi

  "$runuser_bin" -u "$service_user" -- env \
    YINSHI_APP_ROOT="$app_root" \
    YINSHI_PI_UPDATE_STATUS_PATH="$status_path" \
    "$app_root/scripts/update-pi-package.sh"

  updated="$(STATUS_PATH="$status_path" node --input-type=module <<'NODE'
import fs from "node:fs";
const statusPath = process.env.STATUS_PATH;
let updated = false;
try {
  const payload = JSON.parse(fs.readFileSync(statusPath, "utf-8"));
  updated = payload.updated === true;
} catch {
  updated = false;
}
process.stdout.write(updated ? "true" : "false");
NODE
)"

  if [[ "$updated" == "true" ]]; then
    systemctl restart yinshi-sidecar.service
    systemctl restart yinshi-backend.service
  fi
  exit 0
fi

package_name="${PI_PACKAGE_NAME:-@mariozechner/pi-coding-agent}"
app_root="${YINSHI_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
sidecar_dir="${YINSHI_SIDECAR_DIR:-$app_root/sidecar}"
container_image="${YINSHI_CONTAINER_IMAGE:-yinshi-sidecar:latest}"
runtime_dir="${YINSHI_RUNTIME_DIR:-$app_root/.runtime}"
status_path="${YINSHI_PI_UPDATE_STATUS_PATH:-$runtime_dir/pi-package-update.json}"
lock_path="${YINSHI_PI_UPDATE_LOCK_PATH:-$runtime_dir/pi-package-update.lock}"

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

write_status() {
  local status_value="$1"
  local updated_value="$2"
  local previous_version="$3"
  local current_version="$4"
  local latest_version="$5"
  local message_value="$6"

  STATUS_PATH="$status_path" \
  STATUS_VALUE="$status_value" \
  UPDATED_VALUE="$updated_value" \
  PREVIOUS_VERSION="$previous_version" \
  CURRENT_VERSION="$current_version" \
  LATEST_VERSION="$latest_version" \
  MESSAGE_VALUE="$message_value" \
  node --input-type=module <<'NODE'
import fs from "node:fs";
import path from "node:path";

const statusPath = process.env.STATUS_PATH;
if (!statusPath) {
  throw new Error("STATUS_PATH is required");
}
const payload = {
  checked_at: new Date().toISOString(),
  status: process.env.STATUS_VALUE || "unknown",
  previous_version: process.env.PREVIOUS_VERSION || null,
  current_version: process.env.CURRENT_VERSION || null,
  latest_version: process.env.LATEST_VERSION || null,
  updated: process.env.UPDATED_VALUE === "true",
  message: process.env.MESSAGE_VALUE || null,
};
fs.mkdirSync(path.dirname(statusPath), { recursive: true });
const temporaryPath = `${statusPath}.tmp`;
fs.writeFileSync(temporaryPath, `${JSON.stringify(payload, null, 2)}\n`, "utf-8");
fs.renameSync(temporaryPath, statusPath);
NODE
}

read_installed_version() {
  local package_root="$1"
  PACKAGE_ROOT="$package_root" PACKAGE_NAME="$package_name" node --input-type=module <<'NODE'
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const packageRoot = process.env.PACKAGE_ROOT;
const packageName = process.env.PACKAGE_NAME;
if (!packageRoot || !packageName) {
  process.exit(2);
}
const require = createRequire(path.join(packageRoot, "src", "sidecar.js"));
let entryPath;
try {
  entryPath = require.resolve(packageName);
} catch {
  process.exit(3);
}
let currentPath = path.dirname(entryPath);
while (true) {
  const packageJsonPath = path.join(currentPath, "package.json");
  if (fs.existsSync(packageJsonPath)) {
    const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf-8"));
    if (packageJson.name === packageName) {
      process.stdout.write(packageJson.version || "");
      process.exit(packageJson.version ? 0 : 4);
    }
  }
  const parentPath = path.dirname(currentPath);
  if (parentPath === currentPath) {
    break;
  }
  currentPath = parentPath;
}
process.exit(5);
NODE
}

copy_sidecar_context() {
  local target_dir="$1"
  cp "$sidecar_dir/package.json" "$target_dir/package.json"
  cp "$sidecar_dir/package-lock.json" "$target_dir/package-lock.json"
  cp "$sidecar_dir/Dockerfile" "$target_dir/Dockerfile"
  cp -R "$sidecar_dir/src" "$target_dir/src"
}

run_smoke_test() {
  local target_dir="$1"
  (cd "$target_dir" && node --input-type=module <<'NODE'
import { pathToFileURL } from "node:url";
import { AuthStorage, ModelRegistry } from "@mariozechner/pi-coding-agent";

const registry = new ModelRegistry(AuthStorage.inMemory(), null);
const models = registry.getAll();
if (!Array.isArray(models)) {
  throw new Error("Model registry did not return an array");
}
if (models.length === 0) {
  throw new Error("Model registry returned no models");
}
const sidecarModule = await import(pathToFileURL(`${process.cwd()}/src/sidecar.js`).href);
if (typeof sidecarModule.YinshiSidecar !== "function") {
  throw new Error("YinshiSidecar export is missing");
}
const sidecar = new sidecarModule.YinshiSidecar();
sidecar.cleanup();
NODE
  )
}

replace_node_modules() {
  local source_node_modules="$1"
  local target_node_modules="$2"
  local next_node_modules="$target_node_modules.next"
  local previous_node_modules="$target_node_modules.previous"

  rm -rf "$next_node_modules" "$previous_node_modules" || return 1
  cp -a "$source_node_modules" "$next_node_modules" || return 1
  if [[ -d "$target_node_modules" ]]; then
    mv "$target_node_modules" "$previous_node_modules" || {
      rm -rf "$next_node_modules"
      return 1
    }
  fi
  mv "$next_node_modules" "$target_node_modules" || {
    if [[ -d "$previous_node_modules" ]]; then
      mv "$previous_node_modules" "$target_node_modules" || true
    fi
    rm -rf "$next_node_modules"
    return 1
  }
  rm -rf "$previous_node_modules" || return 1
}

previous_version=""
latest_version=""
resolved_version=""
temporary_dir=""

fail_update() {
  local message_value="$1"
  write_status "failed" "false" "$previous_version" "$resolved_version" "$latest_version" "$message_value"
  echo "$message_value" >&2
  exit 1
}

mkdir -p "$runtime_dir"
if ! command -v flock >/dev/null 2>&1; then
  fail_update "flock is required"
fi
exec 9>"$lock_path"
if ! flock -n 9; then
  log "Another pi package update is already running; exiting."
  exit 0
fi

cleanup() {
  if [[ -n "$temporary_dir" && -d "$temporary_dir" ]]; then
    rm -rf "$temporary_dir"
  fi
}
trap cleanup EXIT

if [[ ! -d "$sidecar_dir" ]]; then
  fail_update "Sidecar directory not found: $sidecar_dir"
fi
if ! command -v npm >/dev/null 2>&1; then
  fail_update "npm is required"
fi
if ! command -v podman >/dev/null 2>&1; then
  fail_update "podman is required to rebuild tenant sidecar image"
fi

previous_version="$(read_installed_version "$sidecar_dir" || true)"
if ! latest_version="$(npm view "$package_name" version --silent)"; then
  fail_update "npm view failed for $package_name"
fi
if [[ -z "$latest_version" ]]; then
  fail_update "npm did not return a latest version for $package_name"
fi

if [[ -n "$previous_version" && "$previous_version" == "$latest_version" ]]; then
  write_status "current" "false" "$previous_version" "$previous_version" "$latest_version" "$package_name is already current"
  log "$package_name is already current at $latest_version."
  exit 0
fi

temporary_dir="$(mktemp -d)"
copy_sidecar_context "$temporary_dir"
log "Updating $package_name from ${previous_version:-not installed} to $latest_version in temporary build context."
if ! npm install --prefix "$temporary_dir" --omit=dev --no-audit --no-fund "$package_name@$latest_version"; then
  fail_update "npm install failed for $package_name@$latest_version"
fi
resolved_version="$(read_installed_version "$temporary_dir")"
if [[ "$resolved_version" != "$latest_version" ]]; then
  fail_update "Resolved $resolved_version instead of npm latest $latest_version"
fi

if ! run_smoke_test "$temporary_dir"; then
  fail_update "Sidecar smoke test failed for $package_name@$latest_version"
fi
if ! podman build -t "$container_image" "$temporary_dir"; then
  fail_update "Podman image build failed for $container_image"
fi
if ! replace_node_modules "$temporary_dir/node_modules" "$sidecar_dir/node_modules"; then
  fail_update "Failed to replace host sidecar node_modules"
fi
write_status "updated" "true" "$previous_version" "$resolved_version" "$latest_version" "Updated $package_name to $resolved_version and rebuilt $container_image"
log "Updated $package_name to $resolved_version and rebuilt $container_image."
