"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../src/tender_downloader/webui/static/app.js"), "utf8");
const context = vm.createContext({document: {addEventListener() {}}});
vm.runInContext(source.replace(/\n\}\)\(\);\n$/, `
  globalThis.model = queryProgressModel;
})();\n`), context);
const start = Date.parse("2026-09-11T04:15:15+08:00");
const stamp = seconds => new Date(start + seconds * 1000).toISOString();
const query = {id: "current-query", started_at: stamp(0)};
const running = {operation: "query", running: true, started_at: stamp(0)};

function sourcesAt(seconds, notices = 15) {
  return [
    {source: "吉林平台", status: "ok", notices: 58, details_ok: 58, attachments_found: 57, updated_at: stamp(314)},
    {source: "中采网", status: "running", notices, details_ok: notices, attachments_found: 2, updated_at: stamp(seconds)},
    {source: "归档", status: "queued"},
    {source: "全国平台", status: "queued"},
  ];
}

test("live counts and elapsed time advance even when the task log has no new lines", () => {
  const first = context.model(query, sourcesAt(380), running, start + 385000);
  const next = context.model(query, sourcesAt(392, 17), running, start + 397000);
  assert.equal(first.processed, 1);
  assert.equal(first.total, 4);
  assert.equal(first.notices, 73);
  assert.equal(next.notices, 75);
  assert.equal(next.details, 75);
  assert.equal(next.links, 59);
  assert.equal(next.elapsed, "6分37秒");
  assert.match(next.current, /第 2 \/ 4 个来源：中采网/);
  assert.equal(next.updated, "最近进展：5秒前");
  assert.equal(next.message, "");
});

test("a long source wait is shown as uncertain waiting, distinct from a broken progress connection", () => {
  const now = start + 500000;
  const waiting = context.model(query, sourcesAt(400), running, now, {syncedAt: now});
  assert.equal(waiting.label, "等待新进展");
  assert.match(waiting.message, /可能正在等待源站响应或重试/);
  const disconnected = context.model(query, sourcesAt(499), running, now, {readError: "offline"});
  assert.equal(disconnected.label, "进度连接异常");
  assert.match(disconnected.message, /不能据此判断任务已停止/);
  const stalledPolling = context.model(query, sourcesAt(490), running, now, {syncedAt: now - 21000});
  assert.equal(stalledPolling.label, "进度连接异常");
});

test("an active connector with partial coverage is not counted as finished", () => {
  const rows = sourcesAt(380);
  rows[1].status = "partial";
  const result = context.model(query, rows, running, start + 385000);
  assert.equal(result.activeIndex, 1);
  assert.equal(result.processed, 1);
  assert.equal(result.label, "正在采集");
});

test("completion freezes elapsed time and explicitly distinguishes skipped or failed sources", () => {
  const rows = sourcesAt(380);
  rows[1].status = "ok";
  rows[2] = {source: "归档", status: "unsupported", updated_at: stamp(381)};
  rows[3] = {source: "全国平台", status: "ok", updated_at: stamp(1000)};
  const finished = {operation: "query", running: false, started_at: stamp(0), finished_at: stamp(1001)};
  const result = context.model(query, rows, finished, start + 2000000);
  assert.equal(result.processed, 4);
  assert.equal(result.skipped, 1);
  assert.equal(result.elapsed, "16分41秒");
  assert.equal(result.label, "查询结束 · 有跳过");
  rows[3].status = "failed";
  assert.equal(context.model(query, rows, finished, start + 2000000).label, "查询未完成");
  rows[3].status = "interrupted";
  assert.equal(context.model(query, rows, finished, start + 2000000).processed, 3);
});

test("another operation cannot make an old query appear active and empty history stays hidden", () => {
  assert.equal(context.model(null, [], running, start), null);
  const unrelated = {operation: "download", running: true, started_at: stamp(600)};
  assert.equal(context.model(query, sourcesAt(400), unrelated, start + 650000).running, false);
});

test("human verification remains active and permits already collected exports", () => {
  const rows=sourcesAt(380);rows[1].status='waiting_verification';rows[1].message='请完成验证码；已有结果可导出';
  const result=context.model(query,rows,running,start+700000);
  assert.equal(result.label,'等待人工验证');assert.equal(result.activeIndex,1);
  assert.equal(result.processed,1);assert.equal(result.notices,73);
  assert.match(result.message,/已有结果可导出/);
});
