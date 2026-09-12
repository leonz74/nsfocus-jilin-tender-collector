"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function harness(fetch) {
  const source = fs.readFileSync(path.resolve(__dirname, "../src/tender_downloader/webui/static/app.js"), "utf8");
  const context = vm.createContext({fetch, Blob, URL, document: {addEventListener() {}, querySelector: () => ({content: "csrf"})}});
  vm.runInContext(source.replace(/\n\}\)\(\);\n$/, `
    const downloads = [];
    triggerBlobDownload = (blob, filename) => downloads.push({blob, filename});
    showToast = () => {};
    setButtonsBusy = (buttons, busy) => buttons.forEach(button => {button.disabled = busy;});
    const criteria = {start_date: '2026-01-01', end_date: '2026-09-11', city: '', industry: '', keyword: '', notice_type: ''};
    activeQuery = {id: 'current-query', criteria};
    elements.startDate = {value: criteria.start_date}; elements.endDate = {value: criteria.end_date};
    for (const key of ['catalogSearch', 'catalogTypeFilter', 'catalogCityFilter', 'catalogIndustryFilter']) elements[key] = {value: ''};
    elements.catalogExport = {}; elements.catalogExportScope = {}; elements.catalogExportStatus = {};
    globalThis.h = {elements, downloads, exportNoticeCatalog, updateCatalogExportPresentation, isNoticeSelectable, isNoticeDownloadable,
      setRows(rows, selected = []) {filteredNoticeCatalog = rows; selectedNoticeIdentities = new Set(selected);},
      setPage(page) {catalogPage = page;}
    };
  })();\n`), context);
  return context.h;
}

function csvResponse() {
  return {ok: true, headers: new Headers({'content-type': 'text/csv; charset=utf-8', 'X-Export-Notices': '1', 'X-Export-Rows': '2', 'X-Export-URL-Rows': '2'}),
    blob: async () => new Blob(['项目,下载URL\n测试,https://example.test/a.pdf\n'])};
}

test("export sends the current query and selected visible rows, including already downloaded notices", async () => {
  const requests = [];
  const h = harness(async (url, options) => {requests.push({url, ...options}); return csvResponse();});
  h.setRows([{identity: 'one', downloadStatus: 'downloaded'}, {identity: 'two'}], ['one', 'hidden-old-selection']);
  h.updateCatalogExportPresentation();
  assert.match(h.elements.catalogExportScope.textContent, /已勾选的 1 条/);
  assert.equal(h.isNoticeSelectable({identity: 'one', downloadStatus: 'downloaded'}), true);
  assert.equal(h.isNoticeDownloadable({identity: 'one', downloadStatus: 'downloaded'}), false);
  await h.exportNoticeCatalog();
  assert.equal(requests[0].url, '/api/notices/export');
  assert.deepEqual(JSON.parse(requests[0].body), {query_id: 'current-query', notice_ids: ['one']});
  assert.equal(requests[0].headers['X-CSRF-Token'], 'csrf');
  assert.equal(h.downloads.length, 1);
  assert.match(h.downloads[0].filename, /^标讯信息及下载URL_\d{8}\.csv$/);
  assert.match(h.elements.catalogExportStatus.textContent, /共 2 行，其中 2 行含附件下载 URL/);
});

test("unselected export includes all filtered pages and cannot be submitted twice while pending", async () => {
  let finish;
  const requests = [];
  const h = harness((url, options) => {requests.push(JSON.parse(options.body)); return new Promise(resolve => {finish = resolve;});});
  h.setRows(Array.from({length: 125}, (_, i) => ({identity: String(i)})));
  h.setPage(2);
  const pending = h.exportNoticeCatalog();
  h.updateCatalogExportPresentation(); // A polling update must keep the button disabled.
  assert.equal(h.elements.catalogExport.disabled, true);
  await h.exportNoticeCatalog();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].notice_ids.length, 125);
  finish(csvResponse());
  await pending;
  assert.equal(h.elements.catalogExport.disabled, false);
});

test("empty or changed conditions never export, and server errors remain visible without a fake CSV", async () => {
  let calls = 0;
  const h = harness(async () => {calls++; return {ok: false, status: 422, json: async () => ({error: '查询结果已更新，请刷新后重新选择导出。'})};});
  h.setRows([]);
  await h.exportNoticeCatalog();
  h.setRows([{identity: 'one'}]);
  h.elements.catalogCityFilter.value = '长春';
  await h.exportNoticeCatalog();
  assert.equal(calls, 0);
  h.elements.catalogCityFilter.value = '';
  await h.exportNoticeCatalog();
  assert.equal(calls, 1);
  assert.equal(h.downloads.length, 0);
  assert.match(h.elements.catalogExportStatus.textContent, /查询结果已更新/);
});
