// Covers managed device authorization selection and transient code reporting without provider network calls.

import assert from "node:assert/strict";
import test from "node:test";

import { YinshiSidecar } from "../src/sidecar.js";

function writableMessages() {
  const messages = [];
  return {
    messages,
    socket: {
      writable: true,
      write(value) {
        messages.push(JSON.parse(String(value).trim()));
        return true;
      },
    },
  };
}

function modelRegistryFactory({ options, notification, selectedMethods }) {
  return async () => ({
    modelRuntime: {
      getProvider() {
        return {
          auth: { oauth: {} },
          usesCallbackServer: true,
        };
      },
      async login(_providerId, _authType, interaction) {
        selectedMethods.push(
          await interaction.prompt({
            type: "select",
            message: "Select login method",
            options,
          }),
        );
        interaction.notify(notification);
        return {
          type: "oauth",
          access: "test-access",
          refresh: "test-refresh",
          expires: Date.now() + 60_000,
        };
      },
    },
  });
}

test("browser OAuth remains the default without a managed policy", async () => {
  const selectedMethods = [];
  const sidecar = new YinshiSidecar({
    modelRegistryFactory: modelRegistryFactory({
      options: [
        { id: "browser", label: "Browser login" },
        { id: "device_code", label: "Device code login" },
      ],
      notification: {
        type: "auth_url",
        url: "https://auth.openai.com/oauth/authorize",
        instructions: "Complete browser authorization.",
      },
      selectedMethods,
    }),
  });
  const { messages, socket } = writableMessages();

  await sidecar.startOAuthFlow("request-1", socket, "openai-codex");

  assert.deepEqual(selectedMethods, ["browser"]);
  assert.equal(messages[0].authorization_mode, "browser");
  assert.equal(messages[0].user_code, null);
  assert.equal(messages[0].manual_input_required, true);
  sidecar.clearOAuthFlow("clear-1", socket, messages[0].flow_id);
});

test("managed OAuth selects device authorization and reports its transient code", async () => {
  const selectedMethods = [];
  const sidecar = new YinshiSidecar({
    oauthLoginMode: "device_code",
    modelRegistryFactory: modelRegistryFactory({
      options: [
        { id: "browser", label: "Browser login" },
        { id: "device_code", label: "Device code login" },
      ],
      notification: {
        type: "device_code",
        userCode: "TEST-CODE",
        verificationUri: "https://auth.openai.com/codex/device",
      },
      selectedMethods,
    }),
  });
  const { messages, socket } = writableMessages();

  await sidecar.startOAuthFlow("request-1", socket, "openai-codex");

  assert.deepEqual(selectedMethods, ["device_code"]);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].type, "oauth_started", messages[0].error);
  assert.equal(messages[0].authorization_mode, "device_code");
  assert.equal(messages[0].user_code, "TEST-CODE");
  assert.equal(messages[0].auth_url, "https://auth.openai.com/codex/device");
  assert.equal(messages[0].manual_input_required, false);
  assert.equal(messages[0].manual_input_prompt, null);

  sidecar.handleOAuthStatus("status-1", socket, messages[0].flow_id);
  assert.equal(messages[1].authorization_mode, "device_code");
  assert.equal(messages[1].user_code, "TEST-CODE");
  assert.equal(messages[1].manual_input_required, false);

  sidecar.submitOAuthFlowInput(
    "submit-1",
    socket,
    messages[0].flow_id,
    "unexpected-callback-input",
  );
  assert.equal(messages[2].type, "error");
  assert.equal(messages[2].error, "OAuth flow does not accept manual input");
  sidecar.handleOAuthStatus("status-2", socket, messages[0].flow_id);
  assert.equal(messages[3].manual_input_required, false);
  assert.equal(messages[3].manual_input_submitted, false);
  sidecar.clearOAuthFlow("clear-1", socket, messages[0].flow_id);
});

test("managed OAuth falls back when a provider has no device option", async () => {
  const selectedMethods = [];
  const sidecar = new YinshiSidecar({
    oauthLoginMode: "device_code",
    modelRegistryFactory: modelRegistryFactory({
      options: [{ id: "browser", label: "Browser login" }],
      notification: {
        type: "auth_url",
        url: "https://provider.example/authorize",
        instructions: "Complete browser authorization.",
      },
      selectedMethods,
    }),
  });
  const { messages, socket } = writableMessages();

  await sidecar.startOAuthFlow("request-1", socket, "openai-codex");

  assert.deepEqual(selectedMethods, ["browser"]);
  assert.equal(messages[0].authorization_mode, "browser");
  assert.equal(messages[0].user_code, null);
  assert.equal(messages[0].manual_input_required, true);
  sidecar.clearOAuthFlow("clear-1", socket, messages[0].flow_id);
});
