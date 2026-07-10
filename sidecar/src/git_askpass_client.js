#!/usr/bin/env node

import fs from "node:fs";
import net from "node:net";

const socketPath = process.env.YINSHI_GIT_CREDENTIAL_SOCKET;
const prompt = process.argv[2];
if (typeof socketPath !== "string" || socketPath.length === 0) {
  throw new Error("YINSHI_GIT_CREDENTIAL_SOCKET is required");
}
if (typeof prompt !== "string" || prompt.length === 0 || prompt.length > 1024) {
  throw new Error("Git askpass prompt is invalid");
}

const capabilityBuffer = Buffer.alloc(128);
const capabilityLength = fs.readSync(3, capabilityBuffer, 0, capabilityBuffer.length, 0);
if (capabilityLength <= 0) {
  throw new Error("Git credential capability is required");
}
const capability = capabilityBuffer.subarray(0, capabilityLength).toString("utf-8");
const socket = net.createConnection(socketPath);
let response = "";
socket.setEncoding("utf-8");
socket.setTimeout(5000, () => socket.destroy(new Error("Git credential broker timed out")));
socket.on("connect", () => socket.write(JSON.stringify({ capability, prompt })));
socket.on("data", (chunk) => {
  response += chunk;
  if (response.length > 4096) {
    socket.destroy(new Error("Git credential broker response is too large"));
  }
});
socket.on("end", () => {
  if (response.length === 0) {
    process.stderr.write("Git credential broker denied the request\n");
    process.exitCode = 1;
    return;
  }
  process.stdout.write(response);
});
socket.on("error", (error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
