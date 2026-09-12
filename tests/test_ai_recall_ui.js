"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const criteria = {start_date: "2026-08-01", end_date: "2026-08-31", city: "长春",
  industry: "金融", keyword: "网络安全", notice_type: "", mode: "ai_recall", max_candidates: 100};

function harness(fetch = async () => {throw new Error("unexpected request");}) {
  const source = fs.readFileSync(path.resolve(__dirname, "../src/tender_downloader/webui/static/app.js"), "utf8");
  const context = vm.createContext({fetch, Headers, document: {addEventListener() {},
    querySelector: () => ({content: "csrf"})}});
  vm.runInContext(source.replace(/\n\}\)\(\);\n$/, `
    for (const name of ["startDate", "endDate", "catalogCityFilter", "catalogIndustryFilter", "catalogSearch",
      "catalogTypeFilter", "catalogAiFilter", "catalogDownloadFilter", "queryMode", "queryLimit",
      "queryLimitField", "queryModeHelp", "queryReviewField", "queryReviewSummary", "queryReviewFilter",
      "catalogSummary", "catalogBody", "catalogTableWrap", "catalogEmpty", "catalogPagination",
      "catalogPageLabel", "catalogPrevPage", "catalogNextPage", "catalogState", "catalogEmptyMessage",
      "catalogExportStatus", "apiKey", "queryPlatforms", "queryProgress"]) {
      elements[name] = {value: "", replaceChildren() {}, scrollIntoView() {}};
    }
    renderQueryStatus = () => {};
    createNoticeRows = () => [];
    updateCatalogSelectionPresentation = () => {};
    saveConfiguration = async () => true;
    refreshStatus = async () => {};
    setButtonsBusy = () => {};
    const toasts = [];
    showToast = (...args) => toasts.push(args);
    globalThis.h = {elements, toasts, runPlatformQuery, readQueryCriteria, queryConditionsChanged,
      queryReviewVisible,
      setup(q) {
        activeQuery = {id: "old", criteria: q, sources: ["fixture"]};
        elements.startDate.value=q.start_date; elements.endDate.value=q.end_date;
        elements.catalogCityFilter.value=q.city; elements.catalogIndustryFilter.value=q.industry;
        elements.catalogSearch.value=q.keyword; elements.catalogTypeFilter.value=q.notice_type;
        elements.queryMode.value=q.mode || "standard"; elements.queryLimit.value=q.max_candidates || 100;
        elements.catalogAiFilter.value="all"; elements.queryReviewFilter.value="keep";
      },
      filter(rows, filter="keep") {
        noticeCatalog=rows.map(normaliseNotice); elements.queryReviewFilter.value=filter;
        renderNoticeCatalog(); return filteredNoticeCatalog.map(row => row.identity);
      },
      currentQuery() {return activeQuery;}
    };
  })();\n`), context);
  context.h.setup(criteria);
  return context.h;
}

test("AI matches and uncertain rows survive exact city, industry and keyword mismatches", () => {
  const h = harness();
  const rows = [
    {identity: "rescued", title: "数字化能力提升", city: "吉林省", industry: "其他行业",
      query_review: {status: "match"}},
    {identity: "missing", title: "正文尚未取得", query_review: {status: "unread"}},
    {identity: "failed", title: "模型失败", query_review: {status: "error"}},
    {identity: "negative", title: "学校家具", query_review: {status: "no_match"}},
  ];
  assert.deepEqual(Array.from(h.filter(rows)), ["rescued", "missing", "failed"]);
  assert.deepEqual(Array.from(h.filter(rows, "all")), rows.map(row => row.identity));
  assert.deepEqual(Array.from(h.filter(rows, "suspect")), ["missing", "failed"]);
  assert.match(h.elements.queryReviewSummary.textContent, /待人工复核 2 条/);
  h.elements.catalogCityFilter.value = "吉林市";
  assert.equal(h.queryConditionsChanged(), true);
  assert.equal(h.filter(rows).length, 0, "changed requirements must still trigger a new query");
});

test("changing between regular and AI mode invalidates the old result scope", () => {
  const h = harness();
  assert.equal(h.queryConditionsChanged(), false);
  h.elements.queryMode.value = "standard";
  assert.equal(h.queryConditionsChanged(), true);
  assert.equal(h.readQueryCriteria().mode, undefined);
});

test("query start sends the selected mode, bounded limit and ephemeral key", async () => {
  const requests = [];
  const h = harness(async (url, options) => {
    const body = JSON.parse(options.body);
    requests.push({url, body});
    return {ok: true, text: async () => JSON.stringify({
      query: {id: "new", criteria: body.criteria, sources: ["fixture"], terms: [""]}})};
  });
  h.elements.apiKey.value = "ephemeral-test-key";
  await h.runPlatformQuery({});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/query");
  assert.equal(requests[0].body.criteria.mode, "ai_recall");
  assert.equal(requests[0].body.criteria.max_candidates, 100);
  assert.equal(requests[0].body.api_key, "ephemeral-test-key");
  assert.equal(h.elements.apiKey.value, "");
  assert.equal(h.currentQuery().id, "new");
});

test("missing AI configuration shows an error without replacing the user's old results", async () => {
  const h = harness(async () => ({ok: false, status: 422,
    text: async () => JSON.stringify({error: "请配置可用 AI 接口"})}));
  await h.runPlatformQuery({});
  assert.equal(h.currentQuery().id, "old");
  assert.match(h.toasts[0][1], /AI 接口/);
});

test("security result filter applies in AI recall mode as well", () => {
  const h = harness();
  const rows = [
    {identity: "excluded", ai_decision: "excluded", query_review: {status: "match"}},
    {identity: "pending", ai_decision: "review", query_review: {status: "match"}},
    {identity: "relevant", ai_decision: "relevant", query_review: {status: "match"}},
  ];
  h.elements.catalogAiFilter.value = "excluded";
  assert.deepEqual(Array.from(h.filter(rows)), ["excluded"]);
  h.elements.catalogAiFilter.value = "relevant_pending";
  assert.deepEqual(Array.from(h.filter(rows)), ["pending", "relevant"]);
  h.elements.catalogAiFilter.value = "all";
  assert.deepEqual(Array.from(h.filter(rows)), ["excluded", "pending", "relevant"]);
});
