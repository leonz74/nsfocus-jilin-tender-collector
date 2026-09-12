"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadAiVerificationHarness() {
  const appPath = path.resolve(__dirname, "../src/tender_downloader/webui/static/app.js");
  const marker = "\n})();\n";
  const source = fs.readFileSync(appPath, "utf8");
  assert.ok(source.endsWith(marker), "app.js wrapper marker changed");

  global.document = { addEventListener() {} };
  const instrumented = `${source.slice(0, -marker.length)}
    globalThis.__aiVerificationHarness = {
      elements,
      setPresets(value) { aiPresets = value; },
      markVerified() {
        aiConnectionVerifiedFingerprint = aiConnectionFingerprint();
        aiConnectionVerified = true;
      },
      isVerified: isAiConnectionCurrentlyVerified,
      reset: resetAiConnectionStatus,
    };
  })();\n`;
  vm.runInThisContext(instrumented, { filename: appPath });
  return global.__aiVerificationHarness;
}

test("AI verification survives non-AI redraws but not connection identity changes", () => {
  const harness = loadAiVerificationHarness();
  const noOp = () => {};
  Object.assign(harness.elements, {
    aiProviderPreset: { value: "custom" },
    aiProtocol: { value: "openai_compatible" },
    aiEndpoint: { value: "https://api.example.test/chat/completions" },
    aiModel: { value: "model-a" },
    aiKeyEnv: { value: "TENDER_AI_API_KEY" },
    apiKey: {
      value: "test-key-a",
      classList: { remove: noOp },
      setCustomValidity: noOp,
    },
    aiConnectionStatus: {
      className: "connection-status is-success",
      textContent: "连接成功",
    },
  });

  harness.markVerified();
  harness.reset();
  assert.equal(harness.isVerified(), true, "an unrelated form redraw must preserve verification");
  assert.equal(harness.elements.aiConnectionStatus.textContent, "连接成功");

  harness.elements.aiModel.value = "model-b";
  harness.reset();
  assert.equal(harness.isVerified(), false, "changing the model must invalidate verification");
  assert.equal(harness.elements.aiConnectionStatus.textContent, "尚未测试");

  harness.elements.aiModel.value = "model-a";
  harness.markVerified();
  harness.elements.apiKey.value = "test-key-b";
  harness.reset();
  assert.equal(harness.isVerified(), false, "changing the transient key must invalidate verification");

  harness.elements.apiKey.value = "test-key-a";
  harness.setPresets([{ id: "deepseek", protocol: "openai_compatible" }]);
  harness.elements.aiProviderPreset.value = "deepseek";
  harness.markVerified();
  harness.elements.aiProviderPreset.value = "custom";
  harness.reset();
  assert.equal(harness.isVerified(), false, "changing the provider must invalidate verification");

  delete global.__aiVerificationHarness;
  delete global.document;
});
