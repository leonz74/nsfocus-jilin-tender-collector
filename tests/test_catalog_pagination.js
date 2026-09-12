"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function harness(storage = new Map()) {
  const source = fs.readFileSync(path.resolve(__dirname, "../src/tender_downloader/webui/static/app.js"), "utf8");
  const context = vm.createContext({
    document: {addEventListener() {}},
    window: {localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)}},
  });
  vm.runInContext(source.replace(/\n\}\)\(\);\n$/, `
    for (const name of ["catalogSearch", "catalogTypeFilter", "catalogCityFilter", "catalogIndustryFilter",
      "catalogAiFilter", "catalogDownloadFilter", "catalogSummary", "catalogTableWrap", "catalogEmpty",
      "catalogPagination", "catalogPageLabel", "catalogPrevPage", "catalogNextPage", "catalogState",
      "catalogEmptyMessage", "catalogSelectAll", "catalogSelectionCount", "catalogDownloadSelected",
      "catalogPageSize", "catalogExport", "catalogExportScope"]) elements[name] = {value: ""};
    elements.catalogBody = {replaceChildren(...rows) {this.rows = rows;}};
    elements.catalogSummary.closest = () => ({scrollIntoView() {}});
    elements.catalogAiFilter.value = "all";
    activeQuery = {id: "test-query", criteria: {}};
    renderQueryMode = () => {};
    renderQueryStatus = () => {};
    queryConditionsChanged = () => false;
    createNoticeRows = notice => [notice.identity];
    restoreCatalogPageSize();
    globalThis.h = {
      elements, changeCatalogPageSize, changeCatalogPage, toggleCatalogPageSelection, catalogExportIdentities,
      setRows(rows) {noticeCatalog = rows.map(normaliseNotice); renderNoticeCatalog();},
      filter(value) {elements.catalogDownloadFilter.value = value; renderNoticeCatalog();},
      selected() {return [...selectedNoticeIdentities];},
    };
  })();\n`), context);
  return context.h;
}

const rows = Array.from({length: 67}, (_, i) => ({
  identity: String(i), title: "分页测试 " + i,
  download_status: i < 3 ? "downloaded" : "available",
}));

test("page size defaults to 20, persists a supported choice, and survives unavailable storage", () => {
  const storage = new Map();
  const h = harness(storage);
  h.setRows(rows);
  assert.equal(h.elements.catalogBody.rows.length, 20);
  h.changeCatalogPageSize("10");
  assert.equal(h.elements.catalogBody.rows.length, 10);
  const reloaded = harness(storage);
  reloaded.setRows(rows);
  assert.equal(reloaded.elements.catalogBody.rows.length, 10);
  for (const invalid of ["0", "999", "broken", null]) {
    const invalidStorage = new Map([["nsfocus.catalog.pageSize", invalid]]);
    assert.equal(harness(invalidStorage).elements.catalogPageSize.value, "20");
  }
  const blocked = harness({get() {throw Error("blocked");}, set() {throw Error("blocked");}});
  blocked.setRows(rows);
  blocked.changeCatalogPageSize("50");
  assert.equal(blocked.elements.catalogBody.rows.length, 50);
});

test("resizing resets to page one, keeps cross-page selections and exports all filtered pages", () => {
  const h = harness();
  h.setRows(rows);
  h.changeCatalogPageSize("10");
  h.changeCatalogPage(1);
  assert.deepEqual(Array.from(h.elements.catalogBody.rows), rows.slice(10, 20).map(row => row.identity));
  assert.equal(h.catalogExportIdentities().length, 67);
  h.elements.catalogSelectAll.checked = true;
  h.toggleCatalogPageSelection();
  assert.deepEqual(Array.from(h.selected()), rows.slice(10, 20).map(row => row.identity));
  h.changeCatalogPageSize("50");
  assert.equal(h.elements.catalogBody.rows[0], "0");
  assert.equal(h.elements.catalogBody.rows.length, 50);
  assert.equal(h.selected().length, 10);
  assert.equal(h.catalogExportIdentities().length, 10);
  assert.equal(h.elements.catalogSelectAll.indeterminate, true);
});

test("last pages, fewer filtered results, and empty results keep page bounds and exports correct", () => {
  const h = harness();
  h.setRows(rows);
  h.changeCatalogPage(3);
  assert.equal(h.elements.catalogBody.rows.length, 7);
  assert.match(h.elements.catalogPageLabel.textContent, /第 4 \/ 4 页 · 61–67 条/);
  assert.equal(h.elements.catalogNextPage.disabled, true);
  h.filter("downloaded");
  assert.equal(h.elements.catalogBody.rows.length, 3);
  assert.match(h.elements.catalogPageLabel.textContent, /第 1 \/ 1 页 · 1–3 条/);
  assert.equal(h.elements.catalogPrevPage.disabled, true);
  assert.equal(h.catalogExportIdentities().length, 3);
  h.filter("downloading");
  assert.equal(h.elements.catalogPagination.hidden, true);
  assert.equal(h.elements.catalogEmpty.hidden, false);
  assert.equal(h.catalogExportIdentities().length, 0);
});
