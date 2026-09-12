"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appPath = path.resolve(__dirname, "../src/tender_downloader/webui/static/app.js");
const indexPath = path.resolve(__dirname, "../src/tender_downloader/webui/static/index.html");

function loadCatalogHarness() {
  const marker = "\n})();\n";
  const source = fs.readFileSync(appPath, "utf8");
  assert.ok(source.endsWith(marker), "app.js wrapper marker changed");
  global.document = { addEventListener() {} };
  const instrumented = `${source.slice(0, -marker.length)}
    globalThis.__catalogHarness = {
      normaliseNotice,
      normaliseAiDecision,
      normaliseDownloadStatus,
      extractNoticeItems,
      formatCatalogMoney,
      primaryNoticeAmount,
      csvCell,
      catalogFilterChoices, updateCatalogSelect, CATALOG_CITIES, CATALOG_INDUSTRIES,
      applyCatalogDownloadResponse, preservePendingDownloads, isNoticeDownloadable,
      setCatalog(value) { noticeCatalog = value; },
    };
  })();\n`;
  vm.runInThisContext(instrumented, { filename: appPath });
  return global.__catalogHarness;
}

function cleanupHarness() {
  delete global.__catalogHarness;
  delete global.document;
}

test("city and industry filters remain usable before any records arrive", () => {
  const h = loadCatalogHarness();
  const cities = h.catalogFilterChoices([], h.CATALOG_CITIES);
  assert.ok(cities.some(x => x.value === "长春" && x.count === 0));
  assert.ok(cities.some(x => x.value === "吉林市" && x.count === 0));
  const industries = h.catalogFilterChoices(["教育", "教育", ""], h.CATALOG_INDUSTRIES);
  assert.equal(industries.find(x => x.value === "教育").count, 2);
  assert.equal(industries.find(x => x.value === "医疗卫生").count, 0);
  assert.equal(industries.find(x => x.value === "__unknown__").count, 1);
  cleanupHarness();
});

test("download status responses preserve scanned catalog fields", () => {
  const harness = loadCatalogHarness();
  const notices = [{identity: "one", title: "原项目标题", purchaser: "原采购人", downloadStatus: "downloading"},
                   {identity: "two", title: "另一个项目", downloadStatus: "available"}];
  harness.setCatalog(notices);
  harness.applyCatalogDownloadResponse({results: [{notice_id: "one", status: "partial", files: []}]}, ["one"]);
  assert.equal(notices[0].title, "原项目标题");
  assert.equal(notices[0].purchaser, "原采购人");
  assert.equal(notices[0].downloadStatus, "partial");
  assert.equal(notices[1].downloadStatus, "available");
  cleanupHarness();
});

test("catalog maps workbook-style tender fields without inventing missing values", () => {
  const harness = loadCatalogHarness();
  const notice = harness.normaliseNotice({
    identity: "notice-001",
    data_title: "某单位网络安全服务采购合同公告",
    notice_category: "合同公告",
    customer_name: "某采购单位",
    primary_industry: "教育",
    city: "长春",
    winner_name: "某科技公司",
    award_amount: 824600,
    published_at: "2026-08-21T09:30:00+08:00",
    official_notice_url: "https://example.test/notices/1",
    ai_decision: "relevant",
  });

  assert.equal(notice.identity, "notice-001");
  assert.equal(notice.title, "某单位网络安全服务采购合同公告");
  assert.equal(notice.noticeType, "合同公告");
  assert.equal(notice.purchaser, "某采购单位");
  assert.equal(notice.vendor, "某科技公司");
  assert.equal(notice.purchaserContact, "", "missing contacts stay empty instead of being inferred");
  assert.equal(notice.tenderAmount, undefined, "budget is not copied from the award amount");
  assert.deepEqual(harness.primaryNoticeAmount(notice), {
    value: "824,600 元",
    label: "中标 / 合同",
  });
  cleanupHarness();
});

test("AI and file statuses keep excluded notices out of the default result set", () => {
  const harness = loadCatalogHarness();
  assert.equal(harness.normaliseAiDecision("irrelevant"), "excluded");
  assert.equal(harness.normaliseAiDecision("不相关"), "excluded");
  assert.equal(harness.normaliseAiDecision("relevant"), "relevant");
  assert.equal(harness.normaliseAiDecision("confirmed"), "relevant");
  assert.equal(harness.normaliseAiDecision("needs_review"), "review");
  assert.equal(harness.normaliseDownloadStatus("pending"), "queued");
  assert.equal(harness.normaliseDownloadStatus("completed"), "downloaded");
  assert.equal(harness.normaliseDownloadStatus("download_failed"), "failed");
  cleanupHarness();
});

test("catalog accepts the supported list response envelopes", () => {
  const harness = loadCatalogHarness();
  const items = [{ identity: "one" }];
  assert.equal(harness.extractNoticeItems(items), items);
  assert.equal(harness.extractNoticeItems({ notices: items }), items);
  assert.equal(harness.extractNoticeItems({ items }), items);
  assert.equal(harness.extractNoticeItems({ data: { results: items } }), items);
  cleanupHarness();
});

test("CSV fallback protects spreadsheet formulas and UI advertises on-demand downloads", () => {
  const harness = loadCatalogHarness();
  assert.equal(harness.csvCell("=WEBSERVICE(\"https://example.test\")"), '"\'=WEBSERVICE(""https://example.test"")"');
  cleanupHarness();

  const html = fs.readFileSync(indexPath, "utf8");
  const app = fs.readFileSync(appPath, "utf8");
  for (const id of [
    "notice-catalog",
    "catalog-search",
    "catalog-ai-filter",
    "catalog-select-all",
    "catalog-download-selected",
    "catalog-export",
  ]) {
    assert.match(html, new RegExp(`id=["']${id}["']`));
  }
  assert.match(html, /源文件按需下载/);
  assert.match(html, /点击“下载原文件”或“下载所选”保存/);
  assert.match(app, /identities:\s*batch/);
  assert.match(app, /include_notice:\s*true/);
  assert.match(app, /include_attachments:\s*true/);
  assert.match(app, /next\.delivery\.mode = "on_demand"/);
});


test("live polling does not replace an open city dropdown", () => {
  const h = loadCatalogHarness();
  const select = {value: "长春", dataset: {}, replaceChildren() { throw new Error("must preserve open dropdown"); }};
  global.document.activeElement = select;
  h.updateCatalogSelect(select, ["长春"], "全部城市", h.CATALOG_CITIES);
  assert.equal(select.value, "长春");
  cleanupHarness();
});

test("collection refresh keeps locally queued downloads disabled without hiding new notices", () => {
  const h = loadCatalogHarness();
  const pending = new Map([["one", "downloading"], ["two", "queued"]]);
  const rows = ["one", "two", "new"].map(identity => ({identity, title: "已解析公告", downloadStatus: "not_downloaded"}));
  h.preservePendingDownloads(rows, pending);
  assert.equal(rows[0].downloadStatus, "downloading");
  assert.equal(rows[1].downloadStatus, "queued");
  assert.equal(h.isNoticeDownloadable(rows[0]), false);
  assert.equal(h.isNoticeDownloadable(rows[1]), false);
  assert.equal(h.isNoticeDownloadable(rows[2]), true);
  pending.delete("one");
  const refreshed = [{identity: "one", downloadStatus: "downloaded"}];
  h.preservePendingDownloads(refreshed, pending);
  assert.equal(refreshed[0].downloadStatus, "downloaded");
  cleanupHarness();
});
