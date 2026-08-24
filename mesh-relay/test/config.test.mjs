import test from "node:test";
import assert from "node:assert/strict";
import { loadConfig } from "../src/config.mjs";

test("Mesh stays disabled by default", () => {
  const config = loadConfig({});
  assert.equal(config.enabled, false);
  assert.deepEqual(config.merchants, {});
});

test("Mesh merchant endpoints must be HTTPS", () => {
  assert.throws(() => loadConfig({
    MESH_MERCHANTS_JSON: JSON.stringify({ test: { endpoint: "http://example.com" } }),
  }));
});

test("Enabled relay requires server-side secrets", () => {
  assert.throws(() => loadConfig({ MESH_ENABLED: "true" }), /MESH_RELAY_API_KEY/);
});

