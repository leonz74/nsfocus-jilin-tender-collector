(() => {
  "use strict";

  const SOURCE_META = {
    jilin_ggzy: {
      name: "吉林省公共资源交易平台",
      description: "省级主来源，覆盖政府采购、工程建设与招标文件预公示。",
    },
    ccgp_search: {
      name: "中国政府采购网检索",
      description: "按所选地区、日期窗口和本次关键词检索公告。",
    },
    ccgp_archive: {
      name: "中国政府采购网归档",
      description: "扫描静态归档并按日期、关键词筛选；达到页数上限会标注已完成（可能截断）。",
    },
    national_ggzy: {
      name: "全国公共资源交易平台",
      description: "按所选省市获取汇聚标讯，用于补漏和跨来源交叉校验。",
    },
    url_seed: {
      name: "官方链接补录",
      description: "从 CSV 导入高校、医院、国企等官方公告链接。",
    },
    custom_web: {
      name: "其他网站",
      description: "用户添加的网站，可按公开访问、HTTP Basic 或表单登录方式采集。",
    },
  };

  const OPERATION_LABELS = {
    query: "平台查询",
    sample: "10条试采",
    run: "标讯采集",
    download: "原文件下载",
    verify: "文件校验",
    validate: "配置校验",
    stop: "停止任务",
  };

  const CATALOG_PAGE_SIZES = [10, 20, 50, 100];
  const DEFAULT_CATALOG_PAGE_SIZE = 20;
  const CATALOG_PAGE_SIZE_STORAGE_KEY = "nsfocus.catalog.pageSize";
  const CATALOG_CITIES = ["长春", "吉林市", "四平", "辽源", "通化", "白山", "松原", "白城", "延边", "梅河口", "长白山", "吉林省"];
  const CATALOG_INDUSTRIES = ["党政", "教育", "医疗卫生", "政法公安", "交通物流", "能源电力", "通信广电", "金融", "科研", "制造业", "公共事业", "文旅", "国企综合", "其他行业"];
  let catalogLastLiveRefresh = 0;

  const elements = {};
  let configState = {};
  let aiPresets = [];
  let aiModelCatalog = null;
  let aiModelsRequest = 0;
  let aiKeyState = null;
  let aiKeyStateRequest = 0;
  let aiKeyStatusTimer = null;
  let aiKeyRevision = 0;
  let rawDirty = false;
  let formDirty = false;
  let configLoaded = false;
  let statusPollTimer = null;
  let renderedLogKeys = [];
  let visibleLogLines = [];
  let hiddenLogCount = 0;
  let currentTaskKey = "";
  let bufferTaskKey = "";
  let statusLogBuffer = [];
  let lastLogId = 0;
  let lastStatus = null;
  let browserLoginStates = new Map();
  let browserLoginCheckpoint = { state: "ready", ready: [], pending: [] };
  let aiConnectionVerified = false;
  let aiConnectionVerifiedFingerprint = "";
  let activeQuery = null;
  let querySources = [];
  let queryRestored = false;
  let queryLaunching = false;
  let launchingQueryId = "";
  let queryProgressReadError = "";
  let queryProgressSyncedAt = 0;
  let statusReadFailed = false;
  let noticeCatalog = [];
  let filteredNoticeCatalog = [];
  let selectedNoticeIdentities = new Set();
  let expandedNoticeIdentities = new Set();
  let catalogPage = 1;
  let catalogPageSize = DEFAULT_CATALOG_PAGE_SIZE;
  let regionCatalog = [];
  let regionsReady = false;
  let catalogLoading = false;
  let catalogExportBusy = false;
  let catalogRefreshTaskKey = "";
  let catalogDownloadPollTimer = null;
  let catalogDownloadPollRemaining = 0;
  const pendingDownloadIdentities = new Map();
  let serverDownloadsActive = false;

  document.addEventListener("DOMContentLoaded", initialise);

  async function initialise() {
    cacheElements();
    restoreCatalogPageSize();
    bindEvents();
    setupSectionNavigation();

    const results = await Promise.allSettled([
      loadConfiguration(),
      loadAiPresets(),
      refreshStatus({ silent: true }),
      refreshBrowserLoginStatus({ silent: true }),
      loadRegionCatalog(),
    ]);
    if (results[0].status === "fulfilled") await refreshAiKeyStatus();
    await loadNoticeCatalog({ silent: true });
    if (lastStatus) renderStatus(lastStatus);
    if (!regionsReady) {
      elements.queryPlatforms.disabled = true;
      showPageNotice("地区选项未加载", "请刷新页面重试；地区选项就绪后才能开始查询。", "error");
    }
    window.setInterval(renderQueryProgress, 1000);

    if (results[0].status === "rejected") {
      showPageNotice(
        "无法读取配置",
        humanError(results[0].reason),
        "error",
      );
      showToast("读取失败", humanError(results[0].reason), "error", 8000);
    }

    document.body.classList.remove("is-loading");
    elements.loadingScreen.setAttribute("aria-hidden", "true");
    scheduleStatusPoll(isOperationActive(lastStatus) ? 1400 : 3500);
  }

  function cacheElements() {
    const ids = [
      "loading-screen", "config-form", "start-date", "end-date", "output-dir",
      "database-path", "http-timeout", "http-delay", "http-retries",
      "http-response-mb", "http-download-mb", "http-download-timeout",
      "allow-private-hosts", "source-list", "source-empty", "source-count",
      "add-custom-source",
      "ai-enabled", "ai-toggle-label", "ai-settings-body", "ai-endpoint", "ai-protocol", "ai-model",
      "ai-key-env", "ai-auto-threshold", "ai-review-threshold", "ai-timeout",
      "ai-reclassify", "ai-provider-preset", "ai-model-preset", "ai-preset-status",
      "ai-test-connection", "ai-connection-status",
      "ai-fetch-models", "ai-models-status", "ai-save-key", "ai-delete-key", "ai-key-status",
      "raw-json", "json-state", "json-message", "json-details",
      "format-json", "refresh-json", "apply-json", "api-key", "toggle-api-key",
      "source-credentials", "source-credential-list",
      "auto-scroll", "copy-logs", "clear-logs", "log-output", "log-count",
      "console-placeholder", "summary-date", "summary-sources", "summary-ai",
      "header-status-dot", "header-status-text", "status-dot", "status-title",
      "status-operation", "status-started", "status-finished", "status-exit-code",
      "toast-region", "page-notice", "page-notice-title", "page-notice-message",
      "notice-close",
      "notice-catalog",
      "quick-sample", "beginner-progress", "query-platforms", "query-summary", "query-sources",
      "query-mode", "query-limit", "query-limit-field", "query-review-field", "query-review-filter",
      "query-mode-help", "query-review-summary",
      "query-launch-status",
      "query-progress", "query-progress-nav", "query-progress-current", "query-progress-state", "query-progress-elapsed",
      "query-progress-updated", "query-progress-bar", "query-progress-count", "query-progress-notices",
      "query-progress-details", "query-progress-matches", "query-progress-links", "query-progress-wait",
      "query-progress-matches-label", "query-progress-review",
      "catalog-refresh", "catalog-export", "catalog-export-scope", "catalog-export-status", "catalog-search", "catalog-type-filter",
      "catalog-city-filter", "catalog-industry-filter", "catalog-ai-filter", "catalog-download-filter",
      "catalog-province-filter", "catalog-district-filter", "catalog-region-hint",
      "catalog-reset", "catalog-summary", "catalog-selection-count", "catalog-select-filtered",
      "catalog-download-selected", "catalog-state", "catalog-table-wrap",
      "catalog-select-all", "catalog-body", "catalog-empty", "catalog-empty-message",
      "catalog-pagination", "catalog-prev-page", "catalog-next-page", "catalog-page-label", "catalog-page-size",
    ];

    for (const id of ids) {
      elements[toCamelCase(id)] = document.getElementById(id);
    }

    elements.actionButtons = Array.from(document.querySelectorAll("[data-action]"));
    elements.saveButtons = Array.from(document.querySelectorAll('[data-action="save"]'));
    elements.runButton = document.querySelector('[data-action="run"]');
    elements.stopButton = document.querySelector('[data-action="stop"]');
    elements.verifyButton = document.querySelector('[data-action="verify"]');

    const clearOrphans = document.createElement("button");
    clearOrphans.type = "button";
    clearOrphans.className = "button button-secondary button-small";
    clearOrphans.textContent = "清理已移除网站的登录数据";
    clearOrphans.hidden = true;
    clearOrphans.setAttribute(
      "aria-label",
      "清理已从配置中删除或已更改来源标识的持久浏览器登录数据",
    );
    elements.addCustomSource.insertAdjacentElement("beforebegin", clearOrphans);
    elements.clearOrphanBrowserProfiles = clearOrphans;
  }

  function bindEvents() {
    elements.configForm.addEventListener("input", handleFormChange);
    elements.configForm.addEventListener("change", handleFormChange);
    elements.queryPlatforms.addEventListener("click", () => runPlatformQuery(elements.queryPlatforms));
    for (const input of [elements.startDate, elements.endDate]) input.addEventListener("change", (event) => { handleFormChange(event); renderNoticeCatalog(); });
    elements.quickSample.addEventListener("click", () => runSample(elements.quickSample));
    for (const button of elements.actionButtons) {
      button.addEventListener("click", () => dispatchAction(button.dataset.action, button));
    }

    elements.aiEnabled.addEventListener("change", updateAiPresentation);
    elements.aiProviderPreset.addEventListener("change", handleAiProviderPresetChange);
    elements.aiModelPreset.addEventListener("change", handleAiModelPresetChange);
    elements.aiTestConnection.addEventListener("click", testAiConnection);
    elements.aiFetchModels.addEventListener("click", fetchAiModels);
    elements.aiSaveKey.addEventListener("click", saveAiKey);
    elements.aiDeleteKey.addEventListener("click", deleteAiKey);
    elements.apiKey.addEventListener("input", () => { resetAiConnectionStatus(); invalidateAiModels(); });
    elements.addCustomSource.addEventListener("click", addCustomSource);
    elements.clearOrphanBrowserProfiles.addEventListener(
      "click",
      clearOrphanBrowserProfiles,
    );
    elements.sourceList.addEventListener("click", handleSourceListClick);
    elements.sourceCredentialList.addEventListener("input", (event) => {
      event.target.classList.remove("is-invalid");
      event.target.removeAttribute("aria-invalid");
    });
    elements.rawJson.addEventListener("input", handleRawJsonInput);
    elements.rawJson.addEventListener("keydown", handleJsonEditorKeydown);
    elements.formatJson.addEventListener("click", formatRawJson);
    elements.refreshJson.addEventListener("click", refreshRawJsonFromForm);
    elements.applyJson.addEventListener("click", applyRawJsonToForm);
    elements.toggleApiKey.addEventListener("click", toggleApiKeyVisibility);
    elements.copyLogs.addEventListener("click", copyLogs);
    elements.clearLogs.addEventListener("click", clearVisibleLogs);
    elements.noticeClose.addEventListener("click", () => {
      elements.pageNotice.hidden = true;
    });
    elements.catalogRefresh.addEventListener("click", () => loadNoticeCatalog());
    elements.catalogExport.addEventListener("click", () => exportNoticeCatalog(elements.catalogExport));
    for (const input of [
      elements.catalogSearch,
      elements.catalogTypeFilter,
      elements.catalogIndustryFilter,
      elements.catalogAiFilter,
      elements.catalogDownloadFilter,
      elements.queryMode,
      elements.queryLimit,
      elements.queryReviewFilter,
    ]) {
      input.addEventListener(input === elements.catalogSearch ? "input" : "change", () => {
        catalogPage = 1;
        renderNoticeCatalog();
      });
    }
    for (const input of [elements.catalogProvinceFilter, elements.catalogCityFilter, elements.catalogDistrictFilter]) {
      input.addEventListener("change", () => {
        if (input === elements.catalogProvinceFilter) elements.catalogCityFilter.value = "";
        if (input !== elements.catalogDistrictFilter) elements.catalogDistrictFilter.value = "";
        renderRegionSelectors();
        catalogPage = 1;
        renderNoticeCatalog();
      });
    }
    elements.catalogReset.addEventListener("click", resetCatalogFilters);
    elements.catalogSelectAll.addEventListener("change", toggleCatalogPageSelection);
    elements.catalogSelectFiltered.addEventListener("click", () => {
      selectedNoticeIdentities.clear();
      for (const notice of filteredNoticeCatalog) {
        if (isNoticeSelectable(notice)) selectedNoticeIdentities.add(notice.identity);
      }
      renderNoticeCatalog();
    });
    elements.catalogBody.addEventListener("click", handleCatalogClick);
    elements.catalogBody.addEventListener("change", handleCatalogChange);
    elements.catalogDownloadSelected.addEventListener("click", () => (
      downloadNoticeFiles(Array.from(selectedNoticeIdentities), elements.catalogDownloadSelected)
    ));
    elements.catalogPrevPage.addEventListener("click", () => changeCatalogPage(-1));
    elements.catalogNextPage.addEventListener("click", () => changeCatalogPage(1));
    elements.catalogPageSize.addEventListener("change", () => changeCatalogPageSize(elements.catalogPageSize.value));

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        window.clearTimeout(statusPollTimer);
        refreshStatus({ silent: true })
          .catch(() => null)
          .finally(() => scheduleStatusPoll(1500));
      }
    });

    window.addEventListener("beforeunload", (event) => {
      if (!formDirty && !rawDirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  async function loadConfiguration() {
    const response = await apiRequest("/api/config");
    const data = response && typeof response === "object" && response.config
      ? response.config
      : response;

    if (!isPlainObject(data)) {
      throw new Error("服务返回的配置不是 JSON 对象。");
    }

    configState = deepClone(data);
    populateForm(configState, { markDirty: false, updateRaw: true });
    configLoaded = true;
  }

  async function loadAiPresets() {
    try {
      const response = await apiRequest("/api/ai-presets");
      const presets = Array.isArray(response?.presets) ? response.presets : [];
      aiPresets = presets.filter((preset) => (
        isPlainObject(preset)
        && typeof preset.id === "string"
        && typeof preset.name === "string"
        && typeof preset.endpoint === "string"
      )).map((preset) => ({
        id: preset.id,
        name: preset.name,
        protocol: String(preset.protocol || "openai_compatible"),
        endpoint: preset.endpoint,
        api_key_env: String(preset.api_key_env || "TENDER_AI_API_KEY"),
        models: Array.isArray(preset.models)
          ? preset.models.filter((model) => isPlainObject(model) && typeof model.id === "string")
            .map((model) => ({ id: model.id, label: String(model.label || model.id) }))
          : [],
      }));
      renderAiProviderOptions();
      syncAiPresetSelection(isPlainObject(configState.ai) ? configState.ai : {});
      elements.aiPresetStatus.textContent = aiPresets.length > 0
        ? `已载入 ${aiPresets.length} 个厂商接口；模型列表由你的 Key 实时获取。`
        : "服务暂未提供厂商预设，可以继续使用自定义接口。";
    } catch (error) {
      aiPresets = [];
      renderAiProviderOptions();
      elements.aiPresetStatus.textContent = `厂商预设读取失败：${humanError(error)}；仍可手动填写。`;
    }
  }

  function renderAiProviderOptions() {
    const currentValue = elements.aiProviderPreset.value || "custom";
    elements.aiProviderPreset.replaceChildren(createOption("custom", "自定义接口"));
    for (const preset of aiPresets) {
      elements.aiProviderPreset.append(createOption(preset.id, preset.name));
    }
    elements.aiProviderPreset.value = aiPresets.some((preset) => preset.id === currentValue)
      ? currentValue
      : "custom";
  }

  function renderAiModelOptions(preset, selectedModel = "") {
    const models = aiModelCatalog?.fingerprint === aiModelsFingerprint() ? aiModelCatalog.models : [];
    elements.aiModelPreset.replaceChildren(createOption("custom", selectedModel
      ? `当前填写：${selectedModel}（可手动修改）` : "手动填写模型（高级接口设置）"));
    for (const model of models) elements.aiModelPreset.append(createOption(model.id, model.label));
    elements.aiModelPreset.disabled = false;
    elements.aiModelPreset.value = models.some((model) => model.id === selectedModel) ? selectedModel : "custom";
  }

  function syncAiPresetSelection(ai) {
    const provider = String(ai.provider || "");
    const endpoint = String(ai.endpoint || "");
    const preset = aiPresets.find((item) => item.id === provider)
      || aiPresets.find((item) => item.endpoint === endpoint)
      || null;
    elements.aiProviderPreset.value = preset?.id || "custom";
    renderAiModelOptions(preset, String(ai.model || ""));
  }

  function handleAiProviderPresetChange() {
    resetAiConnectionStatus();
    // Never carry a transient credential over to a different provider.
    elements.apiKey.value = "";
    const preset = getSelectedAiPreset();
    if (!preset) {
      renderAiModelOptions(null);
      elements.aiPresetStatus.textContent = "自定义模式：请直接填写 API Endpoint、模型名称和密钥环境变量。";
      invalidateAiModels();
      scheduleAiKeyStatus();
      return;
    }
    setValue(elements.aiEndpoint, preset.endpoint);
    setValue(elements.aiKeyEnv, preset.api_key_env);
    setValue(elements.aiProtocol, preset.protocol);
    setValue(elements.aiModel, "");
    invalidateAiModels();
    scheduleAiKeyStatus();
    elements.aiPresetStatus.textContent = `已应用 ${preset.name}（${preset.protocol}），可在下方继续微调。`;
  }

  function handleAiModelPresetChange() {
    if (elements.aiModelPreset.value !== "custom") {
      setValue(elements.aiModel, elements.aiModelPreset.value);
    } else {
      elements.aiModel.closest("details").open = true;
      elements.aiModel.focus();
    }
    resetAiConnectionStatus();
  }

  function aiKeyScope() {
    return JSON.stringify([elements.aiProtocol?.value || "openai_compatible",
      elements.aiEndpoint?.value.trim() || "", elements.aiKeyEnv?.value.trim() || ""]);
  }

  function aiModelsFingerprint() {
    return JSON.stringify([aiKeyScope(), elements.apiKey?.value.trim() || "", aiKeyRevision]);
  }

  function hasRememberedAiKey() {
    return aiKeyState?.scope === aiKeyScope() && (aiKeyState.saved || aiKeyState.environment);
  }

  function invalidateAiModels() {
    aiModelsRequest += 1;
    aiModelCatalog = null;
    renderAiModelOptions(null, elements.aiModel.value.trim());
    elements.aiModelsStatus.textContent = "接口或 Key 已变化，请重新获取可用模型。";
    elements.aiModelsStatus.className = "connection-status";
  }

  function scheduleAiKeyStatus() {
    aiKeyState = null;
    aiKeyStateRequest += 1;
    elements.aiKeyStatus.textContent = "正在检查当前接口的 Key 保存状态…";
    elements.aiDeleteKey.hidden = true;
    window.clearTimeout(aiKeyStatusTimer);
    aiKeyStatusTimer = window.setTimeout(refreshAiKeyStatus, 400);
  }

  function renderAiKeyStatus(response, scope) {
    aiKeyState = { ...response, scope };
    elements.aiKeyStatus.className = "connection-status" + (response.saved ? " is-success" : "");
    elements.aiKeyStatus.textContent = response.saved
      ? "已保存到 Mac 钥匙串；输入框留空即可使用，重启后仍有效。"
      : response.environment ? "未保存 Key；当前可使用环境变量中的 Key。"
        : response.supported ? "当前接口尚未保存 Key。" : "当前系统暂不支持保存 Key，可使用临时 Key 或环境变量。";
    elements.aiDeleteKey.hidden = !response.saved;
    elements.aiSaveKey.disabled = response.supported === false;
  }

  async function refreshAiKeyStatus() {
    const scope = aiKeyScope();
    const request = ++aiKeyStateRequest;
    try {
      const { ai, http } = collectAiTestConfiguration();
      const response = await apiRequest("/api/ai-key/status", {method: "POST", body: {ai, http}});
      if (request === aiKeyStateRequest && scope === aiKeyScope()) renderAiKeyStatus(response, scope);
    } catch (error) {
      if (request !== aiKeyStateRequest || scope !== aiKeyScope()) return;
      aiKeyState = null;
      elements.aiKeyStatus.textContent = `保存状态未就绪：${humanError(error)}`;
      elements.aiDeleteKey.hidden = true;
    }
  }

  async function fetchAiModels() {
    const { ai, http } = collectAiTestConfiguration();
    const apiKey = elements.apiKey.value.trim();
    if (!validateAiTestConfiguration(ai, http, apiKey, {requireModel: false})) return;
    const fingerprint = aiModelsFingerprint();
    const request = ++aiModelsRequest;
    setButtonsBusy([elements.aiFetchModels], true);
    elements.aiModelsStatus.className = "connection-status is-testing";
    elements.aiModelsStatus.textContent = "正在按当前 Key 获取模型列表（含分页）…";
    try {
      const response = await apiRequest("/api/ai-models", {method: "POST", body: {api_key: apiKey, ai, http}});
      if (request !== aiModelsRequest || fingerprint !== aiModelsFingerprint()) return;
      if (response?.ok === false) throw new Error(responseMessage(response, "模型列表获取失败"));
      const models = Array.isArray(response.models) ? response.models : [];
      aiModelCatalog = {fingerprint, models};
      renderAiModelOptions(null, elements.aiModel.value.trim());
      elements.aiModelsStatus.className = "connection-status" + (response.complete ? " is-success" : " is-error");
      elements.aiModelsStatus.textContent = `已获取 ${models.length} 个模型（${response.pages || 1} 页）${response.complete ? "" : "，列表不完整"}。${response.message || "请选择模型，再测试连接并保存配置。"}`;
    } catch (error) {
      if (request !== aiModelsRequest || fingerprint !== aiModelsFingerprint()) return;
      aiModelCatalog = null;
      renderAiModelOptions(null, elements.aiModel.value.trim());
      elements.aiModelsStatus.className = "connection-status is-error";
      elements.aiModelsStatus.textContent = `获取失败：${humanError(error)}。可重试，或在高级接口设置中手动填写模型。`;
    } finally {
      setButtonsBusy([elements.aiFetchModels], false);
    }
  }

  async function saveAiKey() {
    const {ai, http} = collectAiTestConfiguration();
    const apiKey = elements.apiKey.value.trim();
    if (!apiKey) {
      showToast("请填写 Key", "输入新 Key 后点击保存；留空会继续使用已保存的 Key。", "warning");
      elements.apiKey.focus();
      return;
    }
    if (!validateAiTestConfiguration(ai, http, apiKey, {requireModel: false})) return;
    const fingerprint = aiModelsFingerprint();
    const scope = aiKeyScope();
    setButtonsBusy([elements.aiSaveKey, elements.aiDeleteKey], true);
    try {
      const response = await apiRequest("/api/ai-key/save", {method: "POST", body: {api_key: apiKey, ai, http}});
      if (scope !== aiKeyScope() || fingerprint !== aiModelsFingerprint()) {
        showToast("Key 已保存", "已保存到刚才的接口；当前输入已改变，请重新检查当前配置。", "success");
        return;
      }
      aiKeyStateRequest += 1;
      aiKeyRevision += 1;
      elements.apiKey.value = "";
      if (elements.apiKey.type === "text") toggleApiKeyVisibility();
      renderAiKeyStatus(response, scope);
      invalidateAiModels();
      resetAiConnectionStatus({force: true});
      showToast("Key 已保存", response.message, "success");
      await fetchAiModels();
    } catch (error) {
      elements.aiKeyStatus.className = "connection-status is-error";
      elements.aiKeyStatus.textContent = `保存失败：${humanError(error)}`;
    } finally {
      setButtonsBusy([elements.aiSaveKey, elements.aiDeleteKey], false);
    }
  }

  async function deleteAiKey() {
    const {ai, http} = collectAiTestConfiguration();
    const scope = aiKeyScope();
    setButtonsBusy([elements.aiSaveKey, elements.aiDeleteKey], true);
    try {
      const response = await apiRequest("/api/ai-key/delete", {method: "POST", body: {ai, http}});
      if (scope !== aiKeyScope()) return;
      aiKeyStateRequest += 1;
      aiKeyRevision += 1;
      renderAiKeyStatus(response, scope);
      invalidateAiModels();
      resetAiConnectionStatus({force: true});
      showToast("已删除保存的 Key", "当前接口的钥匙串记录已删除。", "success");
    } catch (error) {
      elements.aiKeyStatus.textContent = `删除失败：${humanError(error)}`;
    } finally {
      setButtonsBusy([elements.aiSaveKey, elements.aiDeleteKey], false);
    }
  }

  function aiConnectionFingerprint() {
    const selectedPreset = getSelectedAiPreset();
    return JSON.stringify({
      provider: selectedPreset?.id || "custom",
      protocol: selectedPreset?.protocol || elements.aiProtocol?.value || "openai_compatible",
      endpoint: elements.aiEndpoint?.value.trim() || "",
      model: elements.aiModel?.value.trim() || "",
      api_key_env: elements.aiKeyEnv?.value.trim() || "",
      api_key: elements.apiKey?.value.trim() || "",
      key_revision: aiKeyRevision,
    });
  }

  function isAiConnectionCurrentlyVerified() {
    return aiConnectionVerified
      && aiConnectionVerifiedFingerprint !== ""
      && aiConnectionVerifiedFingerprint === aiConnectionFingerprint();
  }

  function resetAiConnectionStatus(options = {}) {
    // Form saves and source edits rebuild parts of the page.  Keep a successful
    // probe when the actual connection identity is unchanged; dates, output
    // paths, source toggles and classification thresholds do not affect it.
    if (options.force !== true && isAiConnectionCurrentlyVerified()) return;
    aiConnectionVerified = false;
    aiConnectionVerifiedFingerprint = "";
    elements.apiKey?.classList.remove("is-invalid");
    elements.apiKey?.setCustomValidity("");
    if (!elements.aiConnectionStatus) return;
    elements.aiConnectionStatus.className = "connection-status";
    elements.aiConnectionStatus.textContent = "尚未测试";
  }

  async function testAiConnection() {
    const { ai, http } = collectAiTestConfiguration();
    const apiKey = elements.apiKey.value.trim();
    if (!ai.enabled) {
      showToast("请先启用 AI 分类", "打开 AI 分类开关后再测试模型连接。", "warning");
      elements.aiEnabled.focus();
      return;
    }
    if (!validateAiTestConfiguration(ai, http, apiKey)) return;

    const testedFingerprint = aiConnectionFingerprint();
    aiConnectionVerified = false;
    aiConnectionVerifiedFingerprint = "";
    setButtonsBusy([elements.aiTestConnection], true);
    elements.aiConnectionStatus.className = "connection-status is-testing";
    elements.aiConnectionStatus.textContent = "正在发送最小测试请求…";
    try {
      const response = await apiRequest("/api/ai-test", {
        method: "POST",
        body: { api_key: apiKey, ai, http },
      });
      if (response?.ok === false) {
        throw new Error(responseMessage(response, "模型服务未通过连接测试。"));
      }
      if (testedFingerprint !== aiConnectionFingerprint()) {
        throw new Error("测试期间模型或 API Key 已更改，请按当前配置重新测试。");
      }
      aiConnectionVerified = true;
      aiConnectionVerifiedFingerprint = testedFingerprint;
      const latency = Number(response?.latency_ms);
      const latencyText = Number.isFinite(latency) ? ` · ${Math.round(latency)} ms` : "";
      elements.aiConnectionStatus.className = "connection-status is-success";
      elements.aiConnectionStatus.textContent = `连接成功：${String(response?.provider || "模型服务")} / ${String(response?.model || elements.aiModel.value)}${latencyText}`;
      showToast("模型连接成功", "厂商、模型和 API Key 均可用。", "success", 5200);
    } catch (error) {
      aiConnectionVerified = false;
      aiConnectionVerifiedFingerprint = "";
      elements.aiConnectionStatus.className = "connection-status is-error";
      elements.aiConnectionStatus.textContent = `连接失败：${humanError(error)}`;
      showToast("模型连接失败", humanError(error), "error", 8500);
    } finally {
      setButtonsBusy([elements.aiTestConnection], false);
    }
  }

  function collectAiTestConfiguration() {
    const ai = isPlainObject(configState.ai) ? deepClone(configState.ai) : {};
    ai.enabled = elements.aiEnabled.checked;
    ai.endpoint = elements.aiEndpoint.value.trim();
    ai.model = elements.aiModel.value.trim();
    ai.api_key_env = elements.aiKeyEnv.value.trim();
    const selectedPreset = getSelectedAiPreset();
    if (selectedPreset) {
      ai.provider = selectedPreset.id;
      ai.protocol = selectedPreset.protocol;
    } else {
      ai.provider = "custom";
      ai.protocol = elements.aiProtocol.value || "openai_compatible";
    }
    assignNumber(ai, "timeout_seconds", elements.aiTimeout);

    const http = isPlainObject(configState.http) ? deepClone(configState.http) : {};
    http.allow_private_hosts = elements.allowPrivateHosts.checked;
    assignNumber(http, "timeout_seconds", elements.httpTimeout);
    assignNumber(http, "max_response_mb", elements.httpResponseMb);
    assignNumber(http, "max_retries", elements.httpRetries);
    return { ai, http };
  }

  function validateAiTestConfiguration(ai, http, apiKey, options = {}) {
    const fields = [
      elements.aiEndpoint,
      elements.aiProtocol,
      elements.aiModel,
      elements.aiKeyEnv,
      elements.aiTimeout,
      elements.apiKey,
    ].filter(Boolean);
    for (const field of fields) {
      field.classList.remove("is-invalid");
      field.setCustomValidity("");
    }

    let firstInvalid = null;
    const markInvalid = (input, message) => {
      input.classList.add("is-invalid");
      input.setCustomValidity(message);
      if (!firstInvalid) firstInvalid = input;
    };

    const endpoint = String(ai.endpoint || "");
    if (!/^https?:\/\//i.test(endpoint) || endpoint.includes("your-model-provider")) {
      markInvalid(elements.aiEndpoint, "请填写有效的模型接口地址");
    } else {
      try {
        const endpointUrl = new URL(endpoint.replace("{model}", "model"));
        const isLoopback = ["127.0.0.1", "localhost", "[::1]"].includes(
          endpointUrl.hostname.toLowerCase(),
        );
        if (endpointUrl.protocol !== "https:"
          && !(isLoopback && http.allow_private_hosts === true)) {
          markInvalid(elements.aiEndpoint, "模型接口必须使用 HTTPS；本机 HTTP 模型需显式允许私网地址");
        }
      } catch (_error) {
        markInvalid(elements.aiEndpoint, "请填写有效的模型接口地址");
      }
    }
    if (options.requireModel !== false && (!String(ai.model || "") || String(ai.model).includes("replace-with"))) {
      markInvalid(elements.aiModel, "请填写模型名称");
    }
    if (!String(ai.api_key_env || "")) {
      markInvalid(elements.aiKeyEnv, "请填写密钥环境变量名");
    }
    if (!Number.isFinite(Number(ai.timeout_seconds)) || Number(ai.timeout_seconds) < 1) {
      markInvalid(elements.aiTimeout, "模型超时必须大于或等于 1 秒");
    }
    if (!apiKey && !hasRememberedAiKey()) {
      markInvalid(elements.apiKey, "请填写或保存 API Key");
    }

    if (!firstInvalid) return true;
    firstInvalid.reportValidity();
    firstInvalid.focus({ preventScroll: true });
    firstInvalid.scrollIntoView({ behavior: "smooth", block: "center" });
    showToast("请检查 AI 配置", firstInvalid.validationMessage, "warning", 5500);
    return false;
  }

  function getSelectedAiPreset() {
    return aiPresets.find((preset) => preset.id === elements.aiProviderPreset.value) || null;
  }

  function createOption(value, label) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }

  function populateForm(data, options = {}) {
    setValue(elements.startDate, data.start_date);
    setValue(elements.endDate, data.end_date);
    setValue(elements.outputDir, data.output_dir ?? "output");
    setValue(elements.databasePath, data.database ?? "output/state.sqlite3");

    const http = isPlainObject(data.http) ? data.http : {};
    setValue(elements.httpTimeout, http.timeout_seconds);
    setValue(elements.httpDelay, http.delay_seconds);
    setValue(elements.httpRetries, http.max_retries);
    setValue(elements.httpResponseMb, http.max_response_mb);
    setValue(elements.httpDownloadMb, http.max_download_mb);
    setValue(elements.httpDownloadTimeout, http.download_timeout_seconds);
    elements.allowPrivateHosts.checked = http.allow_private_hosts === true;

    renderSources(Array.isArray(data.sources) ? data.sources : []);

    const recall = isPlainObject(data.recall) ? data.recall : {};
    const recallMode = recall.mode ?? "p0_complete";
    const recallInput = document.querySelector(`input[name="recall-mode"][value="${cssEscape(recallMode)}"]`);
    for (const input of document.querySelectorAll('input[name="recall-mode"]')) {
      input.checked = input === recallInput;
    }

    const ai = isPlainObject(data.ai) ? data.ai : {};
    elements.aiEnabled.checked = ai.enabled === true;
    setValue(elements.aiEndpoint, ai.endpoint);
    setValue(elements.aiProtocol, ai.protocol ?? "openai_compatible");
    setValue(elements.aiModel, ai.model);
    setValue(elements.aiKeyEnv, ai.api_key_env ?? "TENDER_AI_API_KEY");
    setValue(elements.aiAutoThreshold, ai.auto_accept_threshold);
    setValue(elements.aiReviewThreshold, ai.second_review_threshold);
    setValue(elements.aiTimeout, ai.timeout_seconds);
    elements.aiReclassify.checked = ai.reclassify === true;
    syncAiPresetSelection(ai);
    updateAiPresentation();

    if (aiConnectionVerified && !isAiConnectionCurrentlyVerified()) {
      resetAiConnectionStatus({ force: true });
    }

    if (options.updateRaw !== false) {
      elements.rawJson.value = prettyJson(data);
      rawDirty = false;
      setJsonState("valid", "JSON 有效", "可直接编辑完整 JSON。");
    }

    formDirty = options.markDirty === true;
    updateDirtyPresentation();
    updateSummary();
    clearFieldErrors();
  }

  function renderSources(sources) {
    elements.sourceList.replaceChildren();
    elements.sourceEmpty.hidden = sources.length > 0;

    sources.forEach((source, index) => {
      const sourceData = isPlainObject(source) ? source : {};
      const type = String(sourceData.type ?? `source_${index + 1}`);
      if (type === "custom_web") {
        elements.sourceList.append(createCustomSourceItem(sourceData, index));
        return;
      }
      const meta = SOURCE_META[type] || {
        name: `自定义来源 ${index + 1}`,
        description: "此来源没有预设说明，可在高级 JSON 中查看完整参数。",
      };

      const item = document.createElement("div");
      item.className = `source-item${sourceData.enabled !== false ? " is-enabled" : ""}`;
      item.dataset.sourceIndex = String(index);
      item.append(
        createSourceMain(sourceData, index, meta.name, meta.description, type),
        createRankField(sourceData, index),
      );
      elements.sourceList.append(item);
    });

    updateSourcePresentation();
    renderSourceCredentials(sources);
    renderBrowserLoginStates();
  }

  function createSourceMain(sourceData, index, name, descriptionText, type) {
    const main = document.createElement("div");
    main.className = "source-main";

    const toggleLabel = document.createElement("label");
    toggleLabel.className = "source-toggle";
    toggleLabel.title = `启用或停用${name}`;
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.id = `source-enabled-${index}`;
    toggle.dataset.sourceEnabled = String(index);
    toggle.checked = sourceData.enabled !== false;
    toggle.setAttribute("aria-label", `启用${name}`);
    const check = document.createElement("span");
    check.className = "source-check";
    check.textContent = "✓";
    check.setAttribute("aria-hidden", "true");
    toggleLabel.append(toggle, check);

    const copy = document.createElement("div");
    copy.className = "source-copy";
    const titleRow = document.createElement("div");
    titleRow.className = "source-title-row";
    const title = document.createElement("strong");
    title.textContent = name;
    const typeCode = document.createElement("span");
    typeCode.className = "source-type";
    typeCode.textContent = type;
    titleRow.append(title, typeCode);
    const description = document.createElement("p");
    description.textContent = descriptionText;
    copy.append(titleRow, description);
    main.append(toggleLabel, copy);
    return main;
  }

  function createRankField(sourceData, index) {
    const rankWrap = document.createElement("div");
    rankWrap.className = "rank-field";
    const rankLabel = document.createElement("label");
    rankLabel.htmlFor = `source-rank-${index}`;
    rankLabel.textContent = "权威度";
    const rank = document.createElement("input");
    rank.type = "number";
    rank.id = `source-rank-${index}`;
    rank.dataset.sourceRank = String(index);
    rank.min = "0";
    rank.max = "100";
    rank.step = "1";
    rank.inputMode = "numeric";
    rank.value = numericInputValue(sourceData.authority_rank ?? 50);
    rankWrap.append(rankLabel, rank);
    return rankWrap;
  }

  function createCustomSourceItem(sourceData, index) {
    const item = document.createElement("div");
    item.className = `source-item is-custom${sourceData.enabled !== false ? " is-enabled" : ""}`;
    item.dataset.sourceIndex = String(index);

    const auth = isPlainObject(sourceData.auth) ? sourceData.auth : {};
    const mode = ["none", "browser", "basic", "form"].includes(auth.mode) ? auth.mode : "none";
    const sourceName = String(sourceData.name || `其他网站 ${index + 1}`);
    const description = sourceData.adapter === "okcis"
      ? "已接入条件查询：所选省份或全国、日期、标题关键词。验证码由你完成后自动继续；下载入口可能需要登录。"
      : mode === "none"
      ? "公开访问，不需要登录凭据。"
      : mode === "browser"
        ? "浏览器人工登录；验证码、短信或扫码由用户亲自完成。"
      : mode === "basic"
        ? "HTTP Basic 登录；账号密码仅在运行时输入。"
        : "表单登录；账号密码仅在运行时输入。";

    const summary = document.createElement("div");
    summary.className = "source-item-summary";
    summary.append(
      createSourceMain(sourceData, index, sourceName, description, "custom_web"),
      createRankField(sourceData, index),
    );

    const actions = document.createElement("div");
    actions.className = "source-item-actions";
    const editButton = document.createElement("button");
    editButton.type = "button";
    editButton.dataset.sourceEdit = String(index);
    editButton.setAttribute("aria-expanded", "false");
    editButton.setAttribute("aria-controls", `custom-source-editor-${index}`);
    editButton.textContent = "编辑";
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "source-delete-button";
    deleteButton.dataset.sourceDelete = String(index);
    deleteButton.textContent = "删除";
    actions.append(editButton, deleteButton);
    summary.append(actions);

    const editor = document.createElement("div");
    editor.className = "custom-source-editor";
    editor.id = `custom-source-editor-${index}`;
    editor.hidden = true;

    const identityGrid = document.createElement("div");
    identityGrid.className = "form-grid form-grid-two";
    identityGrid.append(
      createCustomField(index, "name", "网站名称", sourceName, { required: true }),
      createCustomField(index, "id", "来源 ID", sourceData.id || `custom_web_${index + 1}`, {
        required: true,
        help: "3–64 位，以字母或数字开头，可使用点、下划线和短横线；保存后不建议修改。",
      }),
    );

    const urls = createCustomField(
      index,
      "start_urls",
      "入口网址（每行一个）",
      Array.isArray(sourceData.start_urls) ? sourceData.start_urls.join("\n") : "",
      { kind: "textarea", required: true, wide: true, help: "填写公开公告列表页或站内搜索页，必须以 http:// 或 https:// 开头。" },
    );

    const sourceRole = createCustomField(
      index,
      "source_role",
      "来源性质",
      sourceData.source_role || "auto",
      {
        kind: "select",
        wide: true,
        options: [
          ["auto", "自动识别（推荐）"],
          ["official", "中国政府/高校官方站"],
          ["commercial_lead", "商业聚合/线索网站"],
        ],
        help: "线索网站内容永不作为原文交付；工具必须追溯并重新请求官方链接。“官方”仅允许工具内置域，或 HTTPS .gov.cn/.edu.cn 机构入口的精确主机；商业及其他域不能靠手动选择变成官方来源。",
      },
    );

    const authGrid = document.createElement("div");
    authGrid.className = "form-grid form-grid-two";
    const modeField = createCustomField(index, "auth_mode", "登录方式", mode, {
      kind: "select",
      options: [
        ["none", "无需登录"],
        ["browser", "打开登录窗口（推荐）"],
        ["basic", "HTTP Basic 账号密码"],
        ["form", "简单表单账号密码（兼容）"],
      ],
      help: "推荐使用登录窗口：你在浏览器中自行完成验证码、短信或扫码，工具只接管本次 Cookie 会话。",
    });
    authGrid.append(modeField);

    const authFields = document.createElement("div");
    authFields.className = "custom-auth-fields";
    authFields.dataset.authFields = String(index);
    authFields.hidden = !["browser", "form"].includes(mode);
    const authTitle = document.createElement("strong");
    authTitle.textContent = mode === "browser" ? "登录窗口入口" : "简单表单参数";
    const authFormGrid = document.createElement("div");
    authFormGrid.className = "form-grid form-grid-two";
    const loginUrlField = createCustomField(
      index,
      "login_url",
      "登录页 URL",
      auth.login_url || "",
      {
        type: "url",
        required: mode === "form",
        wide: true,
        help: mode === "browser" ? "可留空；留空时打开第一个入口网址。" : "必须与采集入口使用同一 HTTPS 来源。",
      },
    );
    const formOnlyFields = document.createElement("div");
    formOnlyFields.className = "form-grid form-grid-two form-auth-only";
    formOnlyFields.dataset.formAuthFields = String(index);
    formOnlyFields.hidden = mode !== "form";
    formOnlyFields.append(
      createCustomField(index, "username_field", "用户名字段名", auth.username_field || "username", { required: mode === "form" }),
      createCustomField(index, "password_field", "密码字段名", auth.password_field || "password", { required: mode === "form" }),
    );
    authFormGrid.append(loginUrlField);

    const extra = createCustomField(
      index,
      "extra_fields",
      "高级附加字段（JSON）",
      prettyJson(isPlainObject(auth.extra_fields)
        ? auth.extra_fields
        : (isPlainObject(sourceData.extra_fields) ? sourceData.extra_fields : {})),
      { kind: "textarea", wide: true, className: "extra-json-input", help: "可填写表单登录所需的固定隐藏字段；必须是键和值均为字符串的 JSON 对象。" },
    );

    extra.dataset.formAuthExtra = String(index);
    extra.hidden = mode !== "form";
    authFields.append(authTitle, authFormGrid, formOnlyFields, extra);

    const browserPanel = createBrowserLoginPanel(index, sourceData.id || `custom_web_${index + 1}`);
    browserPanel.hidden = mode !== "browser";
    editor.append(identityGrid, urls, sourceRole, authGrid, authFields, browserPanel);
    if (sourceData.adapter === "okcis") {
      editor.append(createCustomField(index, "max_pages", "每个关键词最多查询页数", sourceData.max_pages || 100,
        {type:"number", help:"1–10000 页，默认 100 页。达到上限会标注部分完成；可以提高上限或缩短日期范围。"}));
    }
    item.append(summary, editor);
    updateCustomSourceRequirements(item, sourceData.enabled !== false);
    return item;
  }

  function createCustomField(index, fieldName, labelText, value, options = {}) {
    const wrap = document.createElement("div");
    wrap.className = `field${options.wide ? " field-wide" : ""}`;
    const id = `custom-source-${index}-${fieldName.replaceAll("_", "-")}`;
    const label = document.createElement("label");
    label.htmlFor = id;
    label.textContent = labelText;
    let input;
    if (options.kind === "textarea") {
      input = document.createElement("textarea");
      input.rows = 4;
    } else if (options.kind === "select") {
      input = document.createElement("select");
      for (const [optionValue, optionLabel] of options.options || []) {
        input.append(createOption(optionValue, optionLabel));
      }
    } else {
      input = document.createElement("input");
      input.type = options.type || "text";
      input.autocomplete = "off";
      input.spellcheck = false;
    }
    input.id = id;
    input.dataset.customField = fieldName;
    input.value = String(value ?? "");
    input.required = options.required === true;
    if (options.className) input.classList.add(options.className);
    wrap.append(label, input);
    if (options.help) {
      const help = document.createElement("p");
      help.className = "field-help";
      help.textContent = options.help;
      wrap.append(help);
    }
    return wrap;
  }

  function createBrowserLoginPanel(index, sourceId) {
    const panel = document.createElement("div");
    panel.className = "browser-login-panel";
    panel.dataset.browserLoginPanel = String(index);

    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = "持久浏览器登录";
    const help = document.createElement("p");
    help.textContent = "登录状态按网站和登录域名隔离保存。完成登录或验证码后自动检测并继续；工具不会识别或提交验证码。";
    const state = document.createElement("span");
    state.className = "browser-login-state is-idle";
    state.dataset.browserLoginState = String(sourceId);
    state.textContent = "尚未登录";
    copy.append(title, help, state);

    const actions = document.createElement("div");
    actions.className = "browser-login-actions";
    for (const [action, label, className] of [
      ["start", "打开登录窗口", "button button-secondary button-small"],
      ["focus", "切到登录窗口", "button button-primary button-small"],
      ["clear", "清除本站登录数据", "text-button"],
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = className;
      button.dataset.browserLoginAction = action;
      button.dataset.sourceIndex = String(index);
      button.textContent = label;
      if (action !== "start") button.disabled = true;
      actions.append(button);
    }
    panel.append(copy, actions);
    return panel;
  }

  function collectConfigFromForm(base = configState) {
    const next = isPlainObject(base) ? deepClone(base) : {};
    next.start_date = elements.startDate.value.trim();
    next.end_date = elements.endDate.value.trim();
    next.output_dir = elements.outputDir.value.trim();
    next.database = elements.databasePath.value.trim();

    if (!isPlainObject(next.http)) next.http = {};
    const existingUserAgent = typeof next.http.user_agent === "string"
      ? next.http.user_agent.trim()
      : "";
    const userAgentIsPlaceholder = /replace-with|(?:you|your-email)@example\.com/i.test(existingUserAgent);
    next.http.user_agent = existingUserAgent && !userAgentIsPlaceholder
      ? existingUserAgent
      : "JilinTenderDownloader/0.1";
    assignNumber(next.http, "timeout_seconds", elements.httpTimeout);
    assignNumber(next.http, "delay_seconds", elements.httpDelay);
    assignNumber(next.http, "max_retries", elements.httpRetries);
    assignNumber(next.http, "max_response_mb", elements.httpResponseMb);
    assignNumber(next.http, "max_download_mb", elements.httpDownloadMb);
    assignNumber(next.http, "download_timeout_seconds", elements.httpDownloadTimeout);
    next.http.allow_private_hosts = elements.allowPrivateHosts.checked;

    const sourceBase = Array.isArray(next.sources) ? next.sources : [];
    next.sources = sourceBase.map((source, index) => {
      const updated = isPlainObject(source) ? deepClone(source) : {};
      const item = elements.sourceList.querySelector(`[data-source-index="${index}"]`);
      const enabled = item?.querySelector(`[data-source-enabled="${index}"]`);
      const rank = item?.querySelector(`[data-source-rank="${index}"]`);
      if (enabled) updated.enabled = enabled.checked;
      if (rank) assignNumber(updated, "authority_rank", rank);
      if (updated.type === "custom_web" && item) {
        if (updated.adapter === "okcis") updated.max_pages = Number(customFieldValue(item, "max_pages"));
        updated.id = customFieldValue(item, "id");
        updated.name = customFieldValue(item, "name");
        updated.source_role = customFieldValue(item, "source_role") || "auto";
        updated.start_urls = customFieldValue(item, "start_urls")
          .split(/\r?\n/)
          .map((url) => url.trim())
          .filter(Boolean);
        const mode = customFieldValue(item, "auth_mode") || "none";
        const existingAuth = isPlainObject(updated.auth) ? updated.auth : {};
        updated.auth = { ...existingAuth, mode };
        if (["browser", "form"].includes(mode)) {
          updated.auth.login_url = customFieldValue(item, "login_url");
        } else {
          delete updated.auth.login_url;
        }
        if (mode === "form") {
          updated.auth.username_field = customFieldValue(item, "username_field") || "username";
          updated.auth.password_field = customFieldValue(item, "password_field") || "password";
        } else {
          delete updated.auth.username_field;
          delete updated.auth.password_field;
        }
        const extraInput = item.querySelector('[data-custom-field="extra_fields"]');
        const existingExtra = isPlainObject(updated.auth.extra_fields)
          ? updated.auth.extra_fields
          : (isPlainObject(updated.extra_fields) ? updated.extra_fields : {});
        const parsedExtra = parseJsonObject(extraInput?.value, existingExtra);
        updated.auth.extra_fields = mode === "form"
          ? (parsedExtra.ok ? parsedExtra.value : existingExtra)
          : {};
        delete updated.extra_fields;
      }
      return updated;
    });

    if (!isPlainObject(next.recall)) next.recall = {};
    const recallMode = document.querySelector('input[name="recall-mode"]:checked');
    next.recall.mode = recallMode?.value ?? "p0_complete";

    // Information-first is the UI contract: collection builds the catalog;
    // immutable source bytes are requested only from the catalog actions.
    if (!isPlainObject(next.delivery)) next.delivery = {};
    next.delivery.mode = "on_demand";

    if (!isPlainObject(next.ai)) next.ai = {};
    next.ai.enabled = elements.aiEnabled.checked;
    next.ai.endpoint = elements.aiEndpoint.value.trim();
    next.ai.model = elements.aiModel.value.trim();
    next.ai.api_key_env = elements.aiKeyEnv.value.trim();
    const selectedPreset = getSelectedAiPreset();
    if (selectedPreset) {
      next.ai.provider = selectedPreset.id;
      next.ai.protocol = selectedPreset.protocol;
    } else {
      next.ai.provider = "custom";
      next.ai.protocol = elements.aiProtocol.value || "openai_compatible";
    }
    assignNumber(next.ai, "auto_accept_threshold", elements.aiAutoThreshold);
    assignNumber(next.ai, "second_review_threshold", elements.aiReviewThreshold);
    assignNumber(next.ai, "timeout_seconds", elements.aiTimeout);
    next.ai.reclassify = elements.aiReclassify.checked;

    return next;
  }

  function customFieldValue(item, fieldName) {
    const field = item.querySelector(`[data-custom-field="${fieldName}"]`);
    return String(field?.value ?? "").trim();
  }

  function parseJsonObject(text, fallback = {}) {
    const value = String(text ?? "").trim();
    if (!value) return { ok: true, value: {} };
    try {
      const parsed = JSON.parse(value);
      if (!isPlainObject(parsed)) throw new Error("必须是 JSON 对象");
      return { ok: true, value: parsed };
    } catch (error) {
      return { ok: false, value: isPlainObject(fallback) ? fallback : {}, error: humanError(error) };
    }
  }

  function handleFormChange(event) {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)
      && !(target instanceof HTMLSelectElement)
      && !(target instanceof HTMLTextAreaElement)) return;
    if (target.id === "api-key") return;

    const aiConnectionFieldChanged = [
      elements.aiEndpoint,
      elements.aiProtocol,
      elements.aiModel,
      elements.aiKeyEnv,
    ].includes(target);

    target.classList.remove("is-invalid");
    target.setCustomValidity("");

    if (target === elements.aiEndpoint) {
      const preset = getSelectedAiPreset();
      if (preset && target.value.trim() !== preset.endpoint) {
        elements.aiProviderPreset.value = "custom";
        renderAiModelOptions(null);
        elements.aiPresetStatus.textContent = "接口已手动修改，当前按自定义接口保存。";
      }
    }
    if (target === elements.aiProtocol && getSelectedAiPreset()) {
      elements.aiProviderPreset.value = "custom";
      renderAiModelOptions(null);
      elements.aiPresetStatus.textContent = "协议已手动修改，当前按自定义接口保存。";
    }
    if (target === elements.aiModel) {
      elements.aiModelPreset.value = aiModelCatalog?.models.some((model) => model.id === target.value.trim())
        ? target.value.trim()
        : "custom";
    }
    if (aiConnectionFieldChanged) resetAiConnectionStatus();
    if ([elements.aiEndpoint, elements.aiProtocol, elements.aiKeyEnv].includes(target)) {
      if (target === elements.aiEndpoint || target === elements.aiProtocol) elements.apiKey.value = "";
      invalidateAiModels();
      scheduleAiKeyStatus();
    }

    if (target.matches("[data-source-enabled]")) {
      const item = target.closest(".source-item");
      item?.classList.toggle("is-enabled", target.checked);
      if (item?.classList.contains("is-custom")) updateCustomSourceRequirements(item, target.checked);
    }

    if (target.matches('[data-custom-field="auth_mode"]')) {
      const item = target.closest(".source-item");
      const authFields = item?.querySelector("[data-auth-fields]");
      if (authFields) {
        authFields.hidden = !["browser", "form"].includes(target.value);
        const heading = authFields.querySelector(":scope > strong");
        if (heading) heading.textContent = target.value === "browser" ? "登录窗口入口" : "简单表单参数";
      }
      const formOnly = item?.querySelector("[data-form-auth-fields]");
      if (formOnly) formOnly.hidden = target.value !== "form";
      const formExtra = item?.querySelector("[data-form-auth-extra]");
      if (formExtra) formExtra.hidden = target.value !== "form";
      const browserPanel = item?.querySelector("[data-browser-login-panel]");
      if (browserPanel) browserPanel.hidden = target.value !== "browser";
      updateCustomSourceRequirements(item);
    }

    if (target.matches('[data-custom-field="extra_fields"]')) {
      const result = parseJsonObject(target.value);
      if (!result.ok || !hasOnlyStringEntries(result.value)) {
        target.classList.add("is-invalid");
        target.setCustomValidity(result.ok
          ? "高级附加字段的键和值都必须是字符串"
          : `高级附加字段 JSON 无效：${result.error}`);
      }
    }

    if (rawDirty) {
      const rawResult = parseRawJson(false);
      if (rawResult.ok) {
        configState = rawResult.value;
        rawDirty = false;
      } else {
        formDirty = true;
        setJsonState("invalid", "JSON 有错误", "请先修复高级 JSON，再保存配置。");
        updateDirtyPresentation();
        updateSummaryFromVisibleFields();
        return;
      }
    }

    configState = collectConfigFromForm(configState);
    elements.rawJson.value = prettyJson(configState);
    formDirty = true;
    setJsonState("valid", "JSON 有效", "表单修改已同步到完整 JSON。");
    updateDirtyPresentation();
    updateSourcePresentation();
    updateSummary();
    if (target.matches('[data-source-enabled], [data-custom-field="auth_mode"], [data-custom-field="name"], [data-custom-field="id"]')) {
      renderSourceCredentials(configState.sources);
    }
  }

  function updateCustomSourceRequirements(item) {
    for (const fieldName of ["name", "id", "start_urls"]) {
      const field = item.querySelector(`[data-custom-field="${fieldName}"]`);
      if (field) field.required = true;
    }
    const mode = item.querySelector('[data-custom-field="auth_mode"]')?.value || "none";
    const formLogin = mode === "form";
    for (const fieldName of ["login_url", "username_field", "password_field"]) {
      const field = item.querySelector(`[data-custom-field="${fieldName}"]`);
      if (field) field.required = formLogin;
    }
  }

  function addCustomSource() {
    mutateSources((sources) => {
      const usedIds = new Set(sources.map((source) => String(source?.id || "")));
      let suffix = sources.filter((source) => source?.type === "custom_web").length + 1;
      let id = `custom_web_${suffix}`;
      while (usedIds.has(id)) {
        suffix += 1;
        id = `custom_web_${suffix}`;
      }
      sources.push({
        type: "custom_web",
        id,
        name: `其他网站 ${suffix}`,
        enabled: true,
        authority_rank: 50,
        source_role: "auto",
        start_urls: [],
        auth: { mode: "none", extra_fields: {} },
      });
      return sources.length - 1;
    }, (index) => {
      const editButton = elements.sourceList.querySelector(`[data-source-edit="${index}"]`);
      editButton?.click();
      elements.sourceList.querySelector(`[data-source-index="${index}"] [data-custom-field="name"]`)?.focus();
    });
  }

  function handleSourceListClick(event) {
    const browserButton = event.target.closest("[data-browser-login-action]");
    if (browserButton) {
      void handleBrowserLoginAction(browserButton);
      return;
    }

    const editButton = event.target.closest("[data-source-edit]");
    if (editButton) {
      const index = editButton.dataset.sourceEdit;
      const editor = document.getElementById(`custom-source-editor-${index}`);
      if (!editor) return;
      editor.hidden = !editor.hidden;
      editButton.setAttribute("aria-expanded", String(!editor.hidden));
      editButton.textContent = editor.hidden ? "编辑" : "收起";
      if (!editor.hidden) editor.querySelector("input, textarea, select")?.focus({ preventScroll: true });
      return;
    }

    const deleteButton = event.target.closest("[data-source-delete]");
    if (!deleteButton) return;
    const index = Number(deleteButton.dataset.sourceDelete);
    const source = Array.isArray(configState.sources) ? configState.sources[index] : null;
    const name = source?.name || `其他网站 ${index + 1}`;
    if (!window.confirm(`确定删除“${name}”吗？此操作会在下次保存配置时生效。`)) return;
    mutateSources((sources) => {
      sources.splice(index, 1);
    });
    showToast("已移除网站", `${name} 已从当前配置中移除，请点击保存。`, "info");
  }

  async function handleBrowserLoginAction(button) {
    const item = button.closest(".source-item");
    const sourceId = customFieldValue(item, "id");
    const sourceName = customFieldValue(item, "name") || sourceId;
    if (!sourceId) {
      showToast("请先填写来源 ID", "保存网站配置后才能建立登录会话。", "warning");
      return;
    }
    const action = button.dataset.browserLoginAction;
    if (["start", "focus", "clear"].includes(action)) {
      const saved = await saveConfiguration({ silent: true });
      if (!saved) return;
    }
    if (action === "clear" && !window.confirm(
      `确定清除“${sourceName}”在当前登录域名下的持久登录数据吗？其他网站和浏览器不会受影响。`,
    )) return;
    setButtonsBusy([button], true);
    try {
      const response = await apiRequest(`/api/browser-login/${action}`, {
        method: "POST",
        body: { source_id: sourceId },
      });
      showToast(
        action === "start" ? "登录窗口已打开"
          : action === "focus" ? "已切换登录窗口"
            : "持久登录数据已清除",
        responseMessage(response, action === "start"
          ? `请在新窗口中完成“${sourceName}”登录或验证码；页面会自动检查，无需再点确认。`
          : "会话状态会自动更新。"),
        action === "clear" ? "info" : "success",
        6500,
      );
      await refreshBrowserLoginStatus({ silent: true });
      window.setTimeout(() => refreshBrowserLoginStatus({ silent: true }).catch(() => null), 900);
    } catch (error) {
      showToast("登录窗口操作失败", humanError(error), "error", 8000);
    } finally {
      setButtonsBusy([button], false);
    }
  }

  async function refreshBrowserLoginStatus(options = {}) {
    try {
      const response = await apiRequest("/api/browser-login/status");
      const sessions = Array.isArray(response?.sessions) ? response.sessions : [];
      browserLoginStates = new Map(
        sessions
          .filter((session) => isPlainObject(session) && session.source_id)
          .map((session) => [String(session.source_id), session]),
      );
      const pending = Array.isArray(response?.checkpoint?.pending)
        ? response.checkpoint.pending
        : [];
      browserLoginCheckpoint = isPlainObject(response?.checkpoint)
        ? response.checkpoint
        : { state: pending.length > 0 ? "waiting_for_user" : "ready", ready: [], pending };
      for (const item of pending) {
        if (!isPlainObject(item) || !item.source_id) continue;
        const sourceId = String(item.source_id);
        if (!browserLoginStates.has(sourceId)) browserLoginStates.set(sourceId, item);
      }
      const orphanedCount = Math.max(
        0,
        Number(response?.maintenance?.orphaned_profile_count || 0),
      );
      elements.clearOrphanBrowserProfiles.hidden = orphanedCount === 0;
      elements.clearOrphanBrowserProfiles.textContent = orphanedCount > 0
        ? `清理已移除网站的登录数据（${orphanedCount}）`
        : "清理已移除网站的登录数据";
      renderBrowserLoginStates();
      // The browser-login preflight happens before the child collector exists,
      // so /api/status can legitimately still be "idle".  Re-render the run
      // centre with the combined state instead of telling the user nothing is
      // happening while a login window is actively being monitored.
      if (lastStatus) renderStatus(lastStatus);
      return browserLoginStates;
    } catch (error) {
      if (!options.silent) showToast("登录状态读取失败", humanError(error), "error");
      throw error;
    }
  }

  async function clearOrphanBrowserProfiles() {
    const button = elements.clearOrphanBrowserProfiles;
    if (!window.confirm(
      "确定清理已从当前配置中删除、或来源 ID/登录域名已经改变的网站登录数据吗？当前仍在配置中的网站不会受影响。",
    )) return;
    setButtonsBusy([button], true);
    try {
      const response = await apiRequest("/api/browser-login/clear-orphans", {
        method: "POST",
        body: {},
      });
      const failed = Number(response?.maintenance?.failed_count || 0);
      showToast(
        failed > 0 ? "部分登录数据未能清理" : "旧网站登录数据已清理",
        responseMessage(response, "清理操作已完成。"),
        failed > 0 ? "warning" : "success",
        7500,
      );
      await refreshBrowserLoginStatus({ silent: true });
    } catch (error) {
      showToast("旧网站登录数据清理失败", humanError(error), "error", 8000);
    } finally {
      setButtonsBusy([button], false);
    }
  }

  function renderBrowserLoginStates() {
    for (const panel of elements.sourceList.querySelectorAll("[data-browser-login-panel]")) {
      const item = panel.closest(".source-item");
      const sourceId = customFieldValue(item, "id");
      const session = browserLoginStates.get(sourceId) || { state: "idle" };
      const stateName = String(session.state || "idle").toLowerCase();
      const active = ["starting", "waiting", "verifying", "challenge_required"].includes(stateName);
      const ready = stateName === "ready";
      const state = panel.querySelector("[data-browser-login-state]");
      if (state) {
        state.className = `browser-login-state is-${ready ? "ready" : active ? "active" : stateName === "failed" ? "failed" : "idle"}`;
        if (ready) {
          state.textContent = `登录会话可用 · ${Number(session.cookie_count || 0)} 个 Cookie`;
        } else if (stateName === "starting") {
          state.textContent = "正在启动登录窗口…";
        } else if (stateName === "waiting") {
          state.textContent = "等待在登录窗口中完成登录，完成后自动继续";
        } else if (stateName === "challenge_required") {
          state.textContent = "需要在登录窗口手工完成验证码，完成后自动继续";
        } else if (stateName === "verifying") {
          state.textContent = "正在自动检查登录状态…";
        } else if (stateName === "closed") {
          state.textContent = "窗口已关闭，持久登录数据仍保留";
        } else if (stateName === "cleared") {
          state.textContent = "本站持久登录数据已清除";
        } else if (stateName === "failed") {
          state.textContent = `登录窗口失败：${String(session.error || "请重试")}`;
        } else if (session.profile_saved) {
          state.textContent = "已保存本站隔离登录数据，打开窗口后自动检查";
        } else {
          state.textContent = "尚未登录";
        }
      }
      const start = panel.querySelector('[data-browser-login-action="start"]');
      const focus = panel.querySelector('[data-browser-login-action="focus"]');
      const clear = panel.querySelector('[data-browser-login-action="clear"]');
      if (start) {
        start.disabled = active;
        start.textContent = ready ? "登录窗口已连接" : "打开/恢复登录窗口";
      }
      if (focus) focus.disabled = !(active || ready);
      // A profile may exist after the local app restarts even though no
      // in-memory session exists, so explicit clear must also work from idle.
      if (clear) clear.disabled = stateName === "verifying";
    }
  }

  function mutateSources(mutator, afterRender) {
    const candidateResult = getCandidateConfig();
    if (!candidateResult.ok) return;
    const next = candidateResult.value;
    if (!Array.isArray(next.sources)) next.sources = [];
    const result = mutator(next.sources);
    configState = next;
    populateForm(configState, { markDirty: true, updateRaw: true });
    formDirty = true;
    updateDirtyPresentation();
    afterRender?.(result);
  }

  function renderSourceCredentials(sources) {
    const previous = collectSourceCredentials();
    elements.sourceCredentialList.replaceChildren();
    const loginSources = (Array.isArray(sources) ? sources : []).filter((source) => (
      source?.type === "custom_web"
      && source.enabled !== false
      && ["basic", "form"].includes(source.auth?.mode)
    ));
    elements.sourceCredentials.hidden = loginSources.length === 0;

    for (const [index, source] of loginSources.entries()) {
      const sourceId = String(source.id || `custom_web_${index + 1}`);
      const item = document.createElement("div");
      item.className = "source-credential-item";
      item.dataset.credentialSourceId = sourceId;
      const title = document.createElement("strong");
      title.textContent = String(source.name || sourceId);
      const note = document.createElement("p");
      note.textContent = source.auth?.mode === "basic" ? "HTTP Basic 登录" : "网页表单登录";
      const username = createCredentialInput(sourceId, "username", "账号", "text");
      const password = createCredentialInput(sourceId, "password", "密码", "password");
      username.querySelector("input").value = previous[sourceId]?.username || "";
      password.querySelector("input").value = previous[sourceId]?.password || "";
      item.append(title, note, username, password);
      elements.sourceCredentialList.append(item);
    }
  }

  function createCredentialInput(sourceId, fieldName, labelText, type) {
    const label = document.createElement("label");
    label.textContent = labelText;
    const input = document.createElement("input");
    input.type = type;
    input.autocomplete = type === "password" ? "new-password" : "off";
    input.dataset.credentialSource = sourceId;
    input.dataset.credentialField = fieldName;
    input.setAttribute("aria-label", `${sourceId} ${labelText}`);
    label.append(input);
    return label;
  }

  function collectSourceCredentials() {
    const credentials = {};
    for (const item of elements.sourceCredentialList?.querySelectorAll("[data-credential-source-id]") || []) {
      const sourceId = item.dataset.credentialSourceId;
      const username = item.querySelector('[data-credential-field="username"]')?.value || "";
      const password = item.querySelector('[data-credential-field="password"]')?.value || "";
      credentials[sourceId] = { username, password };
    }
    return credentials;
  }

  function clearTransientCredentials() {
    elements.apiKey.value = "";
    resetAiConnectionStatus();
    for (const input of elements.sourceCredentialList.querySelectorAll("input")) {
      input.value = "";
    }
    if (elements.apiKey.type === "text") toggleApiKeyVisibility();
  }

  function handleRawJsonInput() {
    rawDirty = true;
    formDirty = true;
    updateDirtyPresentation();

    const result = parseRawJson(false);
    if (result.ok) {
      setJsonState("dirty", "等待应用", "JSON 有效；点击“应用到表单”查看可视化结果。");
      elements.rawJson.classList.remove("is-invalid");
    } else {
      setJsonState("invalid", "JSON 有错误", result.error);
      elements.rawJson.classList.add("is-invalid");
    }
  }

  function handleJsonEditorKeydown(event) {
    if (event.key !== "Tab") return;
    event.preventDefault();
    const editor = elements.rawJson;
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    editor.setRangeText("  ", start, end, "end");
    editor.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function formatRawJson() {
    const result = parseRawJson(true);
    if (!result.ok) return;
    elements.rawJson.value = prettyJson(result.value);
    rawDirty = true;
    formDirty = true;
    setJsonState("dirty", "等待应用", "JSON 已格式化；点击“应用到表单”完成同步。");
    updateDirtyPresentation();
  }

  function refreshRawJsonFromForm() {
    if (rawDirty && !parseRawJson(false).ok) {
      showToast("JSON 尚未修复", "“从表单刷新”会覆盖当前错误内容；请先修复或再次点击确认。", "warning", 5200);
      if (elements.refreshJson.dataset.confirmOverwrite !== "true") {
        elements.refreshJson.dataset.confirmOverwrite = "true";
        window.setTimeout(() => delete elements.refreshJson.dataset.confirmOverwrite, 5200);
        return;
      }
    }

    configState = collectConfigFromForm(configState);
    elements.rawJson.value = prettyJson(configState);
    elements.rawJson.classList.remove("is-invalid");
    rawDirty = false;
    formDirty = true;
    setJsonState("valid", "JSON 有效", "已用表单中的值刷新完整 JSON。");
    updateDirtyPresentation();
  }

  function applyRawJsonToForm() {
    const result = parseRawJson(true);
    if (!result.ok) return;
    configState = result.value;
    populateForm(configState, { markDirty: true, updateRaw: false });
    elements.rawJson.value = prettyJson(configState);
    elements.rawJson.classList.remove("is-invalid");
    rawDirty = false;
    formDirty = true;
    setJsonState("valid", "JSON 已应用", "完整 JSON 已同步到可视化表单。");
    updateDirtyPresentation();
    showToast("已应用到表单", "请检查可视化字段，确认后保存配置。", "success");
  }

  function parseRawJson(showFeedback) {
    try {
      const parsed = JSON.parse(elements.rawJson.value);
      if (!isPlainObject(parsed)) {
        throw new Error("配置根节点必须是 JSON 对象。");
      }
      return { ok: true, value: parsed };
    } catch (error) {
      const message = readableJsonError(error);
      if (showFeedback) {
        elements.rawJson.classList.add("is-invalid");
        setJsonState("invalid", "JSON 有错误", message);
        showToast("JSON 无法应用", message, "error", 6000);
        elements.rawJson.focus();
      }
      return { ok: false, error: message };
    }
  }

  async function dispatchAction(action, button) {
    switch (action) {
      case "save":
        await saveConfiguration({ button });
        break;
      case "validate":
        await validateConfiguration(button);
        break;
      case "run":
        await runPlatformQuery(button);
        break;
      case "stop":
        await stopOperation(button);
        break;
      case "verify":
        await verifyFiles(button);
        break;
      case "open-output":
        await openOutputDirectory(button);
        break;
      default:
        break;
    }
  }

  async function saveConfiguration(options = {}) {
    if (!configLoaded) {
      showToast("配置尚未就绪", "请等待配置读取完成后再保存。", "warning");
      return false;
    }

    const candidateResult = getCandidateConfig();
    if (!candidateResult.ok) return false;
    const candidate = candidateResult.value;

    if (!validateVisibleForm(candidate, { requireReady: false })) return false;

    const buttons = options.button ? [options.button] : elements.saveButtons;
    setButtonsBusy(buttons, true);
    try {
      const response = await apiRequest("/api/config", {
        method: "PUT",
        body: candidate,
      });
      const savedConfig = isPlainObject(response?.config) ? response.config : candidate;
      configState = deepClone(savedConfig);
      populateForm(configState, { markDirty: false, updateRaw: true });
      setJsonState("valid", "JSON 有效", "配置已保存到本机。");
      // Enabled browser-login sources can change as part of this save. Refresh
      // the server-side checkpoint immediately so a disabled source's old
      // CAPTCHA state does not keep the run centre showing “waiting”.
      await refreshBrowserLoginStatus({ silent: true }).catch(() => null);
      if (!options.silent) {
        showToast("配置已保存", responseMessage(response, "config.json 已更新。"), "success");
      }
      return true;
    } catch (error) {
      showToast("保存失败", humanError(error), "error", 7000);
      return false;
    } finally {
      setButtonsBusy(buttons, false);
    }
  }

  async function validateConfiguration(button) {
    const saved = await saveConfiguration({ silent: true });
    if (!saved) return;

    setButtonsBusy([button], true);
    try {
      const response = await apiRequest("/api/validate", { method: "POST" });
      if (response?.valid === false) {
        throw new Error(responseMessage(response, "配置校验未通过。"));
      }
      showToast("配置校验通过", responseMessage(response, "日期、来源和模型参数均可使用。"), "success", 5200);
      if (Array.isArray(response?.warnings) && response.warnings.length > 0) {
        const warningMessage = response.warnings.map(String).join("；");
        showPageNotice("配置可用，但有提醒", warningMessage, "warning");
        showToast("配置提醒", warningMessage, "warning", 8000);
      } else {
        elements.pageNotice.hidden = true;
      }
    } catch (error) {
      showToast("配置校验未通过", humanError(error), "error", 8000);
      showPageNotice("配置校验未通过", humanError(error), "error");
    } finally {
      setButtonsBusy([button], false);
      refreshStatus({ silent: true }).catch(() => null);
    }
  }

  async function runCollection(button) {
    const candidateResult = getCandidateConfig();
    if (!candidateResult.ok) return;
    if (!validateVisibleForm(candidateResult.value, { requireReady: true })) return;
    if (candidateResult.value.ai?.enabled && !isAiConnectionCurrentlyVerified()) {
      if (aiConnectionVerified) resetAiConnectionStatus({ force: true });
      showToast(
        "请先测试模型连接",
        "按“厂商 → 模型 → API Key”的顺序填写，并确认测试联通后再运行。",
        "warning",
        7000,
      );
      elements.aiTestConnection.focus();
      elements.aiTestConnection.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    if (!validateTransientSourceCredentials(candidateResult.value.sources)) return;

    const saved = await saveConfiguration({ silent: true });
    if (!saved) return;
    setButtonsBusy([button], true);
    try {
      if (!await ensureBrowserLoginSessions(candidateResult.value.sources)) return;
      const apiKey = elements.apiKey.value;
      const sourceCredentials = collectSourceCredentials();
      const response = await apiRequest("/api/run", {
        method: "POST",
        body: { api_key: apiKey, source_credentials: sourceCredentials },
      });
      clearTransientCredentials();
      showToast("采集任务已启动", responseMessage(response, "实时进度会显示在下方日志中。"), "success");
      hiddenLogCount = 0;
      currentTaskKey = "";
      await refreshStatus({ silent: true }).catch(() => null);
    } catch (error) {
      showToast("无法启动任务", humanError(error), "error", 8000);
    } finally {
      setButtonsBusy([button], false);
    }
  }

  async function runSample(button) {
    const saved = await saveConfiguration({ silent: true });
    if (!saved) return;
    setButtonsBusy([button], true);
    try {
      await apiRequest("/api/sample", {method:"POST"});
      hiddenLogCount = 0;
      currentTaskKey = "";
      showToast("开始试采", "正在读取吉林公共资源最多10条公开公告。清单会自动更新，可边采集边查看。", "success");
      await refreshStatus({silent:true});
    } catch (error) {
      showToast("试采未启动", humanError(error), "error");
    } finally {
      setButtonsBusy([button], false);
    }
  }

  function validateTransientSourceCredentials(sources) {
    const requiredIds = new Set((Array.isArray(sources) ? sources : [])
      .filter((source) => source?.type === "custom_web"
        && source.enabled !== false
        && ["basic", "form"].includes(source.auth?.mode))
      .map((source) => String(source.id || "")));
    let firstInvalid = null;
    for (const item of elements.sourceCredentialList.querySelectorAll("[data-credential-source-id]")) {
      if (!requiredIds.has(item.dataset.credentialSourceId)) continue;
      for (const input of item.querySelectorAll("input")) {
        input.classList.remove("is-invalid");
        input.removeAttribute("aria-invalid");
        if (!input.value) {
          input.classList.add("is-invalid");
          input.setAttribute("aria-invalid", "true");
          firstInvalid ||= input;
        }
      }
    }
    if (!firstInvalid) return true;
    showToast("请填写网站登录凭据", "需要登录的数据源必须填写本次运行的账号和密码。", "warning", 6000);
    firstInvalid.focus();
    firstInvalid.scrollIntoView({ behavior: "smooth", block: "center" });
    return false;
  }

  async function ensureBrowserLoginSessions(sources) {
    const required = (Array.isArray(sources) ? sources : []).filter((source) => (
      source?.type === "custom_web"
      && source.enabled !== false
      && source.auth?.mode === "browser"
    ));
    if (required.length === 0) return true;
    try {
      await refreshBrowserLoginStatus({ silent: true });
    } catch (_error) {
      showToast("无法确认网站登录状态", "请检查本地服务后重试。", "error");
      return false;
    }
    // Recheck sessions that were ready from an earlier run. This is read-only
    // and catches a portal that has since expired or returned to a CAPTCHA.
    for (const source of required) {
      const sourceId = String(source.id || "");
      if (String(browserLoginStates.get(sourceId)?.state || "") !== "ready") continue;
      try {
        await apiRequest("/api/browser-login/probe", {
          method: "POST",
          body: { source_id: sourceId },
        });
      } catch (_error) {
        // The refreshed state below supplies the user-facing result.
      }
    }
    await refreshBrowserLoginStatus({ silent: true }).catch(() => null);
    let missing = required.filter((source) => (
      String(browserLoginStates.get(String(source.id || ""))?.state || "") !== "ready"
    ));
    if (missing.length === 0) return true;

    for (const source of missing) {
      const sourceId = String(source.id || "");
      const state = String(browserLoginStates.get(sourceId)?.state || "idle");
      const action = ["waiting", "challenge_required", "verifying"].includes(state)
        ? "focus"
        : "start";
      try {
        await apiRequest(`/api/browser-login/${action}`, {
          method: "POST",
          body: { source_id: sourceId },
        });
      } catch (error) {
        showToast("无法打开网站登录窗口", humanError(error), "error", 8000);
        return false;
      }
    }
    showToast(
      "等待网站登录或验证码",
      `请在已打开窗口完成：${missing.map((source) => source.name || source.id).join("、")}。验证通过后将自动开始采集。`,
      "warning",
      10000,
    );
    const sourceId = String(missing[0].id || "");
    const item = Array.from(elements.sourceList.querySelectorAll(".source-item.is-custom")).find(
      (candidate) => customFieldValue(candidate, "id") === sourceId,
    );
    const editor = item?.querySelector(".custom-source-editor");
    const editButton = item?.querySelector("[data-source-edit]");
    if (editor?.hidden) editButton?.click();
    item?.scrollIntoView({ behavior: "smooth", block: "center" });

    const deadline = Date.now() + (15 * 60 * 1000);
    while (Date.now() < deadline) {
      try {
        // Long-poll the local service so background-tab timer throttling does
        // not delay continuation after the user finishes a CAPTCHA.
        await apiRequest("/api/browser-login/wait", {
          method: "POST",
          body: { timeout_seconds: 10 },
        });
        await refreshBrowserLoginStatus({ silent: true });
      } catch (_error) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        continue;
      }
      missing = required.filter((source) => (
        String(browserLoginStates.get(String(source.id || ""))?.state || "") !== "ready"
      ));
      if (missing.length === 0) {
        showToast("网站登录已确认", "正在自动启动采集任务。", "success", 4500);
        return true;
      }
      const failed = missing.find((source) => (
        String(browserLoginStates.get(String(source.id || ""))?.state || "") === "failed"
      ));
      if (failed) {
        showToast("网站登录窗口异常", `请重新打开：${failed.name || failed.id}`, "error", 7500);
        return false;
      }
      const closed = missing.find((source) => (
        ["closed", "cleared"].includes(
          String(browserLoginStates.get(String(source.id || ""))?.state || ""),
        )
      ));
      if (closed) {
        showToast(
          "网站登录窗口已关闭",
          `请重新打开：${closed.name || closed.id}；已保存的隔离登录数据不会丢失。`,
          "warning",
          7500,
        );
        return false;
      }
    }
    showToast("等待登录超时", "登录数据仍会保留；请重新点击开始采集继续。", "warning", 7500);
    return false;
  }

  async function stopOperation(button) {
    setButtonsBusy([button], true);
    try {
      const response = await apiRequest("/api/stop", { method: "POST" });
      showToast("正在停止", responseMessage(response, "已发送停止请求，请等待当前步骤结束。"), "warning", 5000);
      await refreshStatus({ silent: true }).catch(() => null);
    } catch (error) {
      showToast("停止失败", humanError(error), "error", 7000);
    } finally {
      setButtonsBusy([button], false);
    }
  }

  async function verifyFiles(button) {
    const saved = await saveConfiguration({ silent: true });
    if (!saved) return;

    setButtonsBusy([button], true);
    try {
      const response = await apiRequest("/api/verify", { method: "POST" });
      showToast("文件校验已启动", responseMessage(response, "校验结果会显示在实时日志中。"), "success");
      hiddenLogCount = 0;
      currentTaskKey = "";
      await refreshStatus({ silent: true }).catch(() => null);
    } catch (error) {
      showToast("无法开始校验", humanError(error), "error", 7000);
    } finally {
      setButtonsBusy([button], false);
    }
  }

  async function openOutputDirectory(button) {
    setButtonsBusy([button], true);
    try {
      const response = await apiRequest("/api/open-output", { method: "POST" });
      showToast("已请求打开目录", responseMessage(response, "请查看访达或文件管理器。"), "info");
    } catch (error) {
      showToast("无法打开目录", humanError(error), "error", 7000);
    } finally {
      setButtonsBusy([button], false);
    }
  }

  function readQueryCriteria() {
    const industry = elements.catalogIndustryFilter.value;
    let keyword = elements.catalogSearch.value.trim();
    if (keyword === industry) keyword = "";
    const criteria = {start_date: elements.startDate.value, end_date: elements.endDate.value,
      city: elements.catalogCityFilter.value, industry, keyword,
      notice_type: elements.catalogTypeFilter.value};
    if (elements.catalogProvinceFilter) {
      criteria.province = elements.catalogProvinceFilter.value;
      criteria.district = elements.catalogDistrictFilter.value;
    }
    if (elements.queryMode?.value === "ai_recall") {
      criteria.mode = "ai_recall";
      criteria.max_candidates = Number(elements.queryLimit.value);
    }
    return criteria;
  }

  function queryConditionsChanged() {
    if (!activeQuery) return false;
    const current = comparableQueryCriteria(readQueryCriteria());
    const previous = comparableQueryCriteria(activeQuery.criteria);
    return [...new Set([...Object.keys(current), ...Object.keys(previous)])]
      .some(key => (current[key] ?? "") !== (previous[key] ?? ""));
  }

  function comparableQueryCriteria(criteria) {
    if (!elements.catalogProvinceFilter) return criteria;
    const value = {...criteria, province: criteria.province ?? "吉林省", district: criteria.district || ""};
    const province = regionCatalog.find(p => p.name === value.province);
    value.city = canonicalCatalogCity(value.city || "", province);
    if (province?.municipality && !value.city) value.city = province.name;
    return value;
  }

  async function loadRegionCatalog() {
    const data = await apiRequest("/api/regions");
    if (!Array.isArray(data.provinces) || !data.provinces.length) throw new Error("地区数据不可用");
    regionCatalog = data.provinces;
    regionsReady = true;
    renderRegionSelectors();
  }

  function canonicalCatalogCity(value, province) {
    if (!value || (value === province?.name && !province?.municipality)) return "";
    if (value === "延边") value = "延边朝鲜族自治州";
    const city = province?.children.find(c => c.name === value || c.name.replace(/市$/, "") === value);
    return city?.name || value;
  }

  function setRegionOptions(select, choices, allLabel, selected) {
    const signature = JSON.stringify([allLabel, choices.map(c => c.name)]);
    if (select.dataset.optionsSignature !== signature) {
      select.replaceChildren(createOption("", allLabel), ...choices.map(c => createOption(c.name, c.name)));
      select.dataset.optionsSignature = signature;
    }
    select.value = choices.some(c => c.name === selected) ? selected : "";
  }

  function renderRegionSelectors() {
    if (!elements.catalogProvinceFilter) return;
    const provinceValue = elements.catalogProvinceFilter.value;
    setRegionOptions(elements.catalogProvinceFilter, regionCatalog, "全国", provinceValue);
    const province = regionCatalog.find(p => p.name === elements.catalogProvinceFilter.value);
    const cityValue = canonicalCatalogCity(elements.catalogCityFilter.value, province);
    setRegionOptions(elements.catalogCityFilter, province?.children || [],
      province?.subdivisions_unavailable ? "暂无下级区划数据" : province ? "全省 / 全部城市" : "请先选择省份", cityValue);
    if (province?.municipality) elements.catalogCityFilter.value = province.name;
    const city = province?.children.find(c => c.name === elements.catalogCityFilter.value);
    setRegionOptions(elements.catalogDistrictFilter, city?.children || [],
      city ? "全部区县" : "请先选择城市", elements.catalogDistrictFilter.value);
    elements.catalogProvinceFilter.disabled = !regionsReady;
    elements.catalogCityFilter.disabled = !province?.children.length || Boolean(province.municipality);
    elements.catalogDistrictFilter.disabled = !city?.children?.length;
    const scope = [province?.name, !province?.municipality && city?.name, elements.catalogDistrictFilter.value].filter(Boolean).join(" / ") || "全国";
    elements.catalogRegionHint.textContent = regionsReady
      ? `当前范围：${scope}。` + (province?.subdivisions_unavailable ? "暂提供省级选项，现有采集来源尚未接入该地区。"
        : "不选下一级表示全部；市县以公告披露信息为准。")
        + (province && !province.subdivisions_unavailable && province.name !== "吉林省" ? "吉林省平台不适用于此范围。" : "")
      : "地区选项尚未加载，请刷新页面重试。";
  }

  function noticeMatchesRegion(notice) {
    const province = elements.catalogProvinceFilter.value;
    const city = elements.catalogCityFilter.value;
    const district = elements.catalogDistrictFilter.value;
    const location = notice.location || {};
    return (!province || location.province === province)
      && (!city || location.city === city)
      && (!district || location.district === district);
  }

  function displaySourceName(name) {
    const names = {"中国政府采购网检索（吉林）": "中国政府采购网检索",
      "中国政府采购网（吉林）": "中国政府采购网归档", "全国公共资源交易平台（吉林）": "全国公共资源交易平台"};
    return names[name] || name;
  }

  function queryReviewPresentation(review) {
    const status = review?.status || "not_run";
    const labels = {match: "AI 判断匹配", suspect: "待人工复核", no_match: "AI 判断不匹配",
      error: "AI 失败待复核", unread: "正文缺失待复核", not_run: "AI 未执行待复核"};
    return {label: labels[status] || labels.suspect,
      className: status === "match" ? "is-success" : status === "no_match" ? "is-muted" : "is-warning"};
  }

  function queryReviewVisible(review, filter = "keep") {
    const status = review?.status || "not_run";
    if (filter === "all") return true;
    if (filter === "keep") return status !== "no_match";
    if (filter === "suspect") return !["match", "no_match"].includes(status);
    return status === filter;
  }

  function renderQueryMode() {
    if (!elements.queryMode) return;
    const selected = elements.queryMode.value === "ai_recall";
    const active = activeQuery?.criteria?.mode === "ai_recall";
    elements.queryLimitField.hidden = !selected;
    elements.queryModeHelp.hidden = !selected && !active;
    elements.queryReviewField.hidden = !active;
    elements.queryReviewSummary.hidden = !active;
    if (active) {
      const matched = noticeCatalog.filter(n => n.queryReview?.status === "match").length;
      const rejected = noticeCatalog.filter(n => n.queryReview?.status === "no_match").length;
      const uncertain = noticeCatalog.length - matched - rejected;
      const broad = noticeCatalog.filter(n => n.queryReview?.origin === "broad").length;
      elements.queryReviewSummary.textContent = `本轮候选 ${noticeCatalog.length} 条（额外广搜 ${broad} 条）· AI 判断匹配 ${matched} 条 · 待人工复核 ${uncertain} 条 · AI 判断不匹配 ${rejected} 条。行业、城市仍展示原始提取值；此模式按 AI 需求复核筛选，未知项保留。可在“全部候选”审计不匹配项。这些数量不是召回率。`;
    }
  }

  function formatProgressDuration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor(total % 3600 / 60);
    return `${hours ? `${hours}小时` : ""}${minutes || hours ? `${minutes}分` : ""}${total % 60}秒`;
  }

  function queryProgressModel(query, sources, status, now = Date.now(), options = {}) {
    if (!query) return null;
    const started = Date.parse(query.started_at) || now;
    const sameRun = status?.operation === "query"
      && Math.abs((Date.parse(status.started_at) || 0) - started) < 5000;
    const running = Boolean(options.launching || (sameRun && isOperationActive(status)));
    let activeIndex = running ? sources.findIndex(source => ["running", "waiting_verification"].includes(source.status)) : -1;
    // A connector may mark its coverage partial while it continues reading.
    // Do not count that active source as finished until execution moves on.
    if (running && activeIndex < 0) {
      for (let i = sources.length - 1; i >= 0; i--) {
        if (sources[i].updated_at) {
          if (sources[i].status === "partial") activeIndex = i;
          break;
        }
      }
    }
    const terminal = new Set(["ok", "empty", "partial", "blocked", "failed", "unsupported"]);
    const processed = sources.filter((source, i) => i !== activeIndex && terminal.has(source.status)).length;
    const incomplete = sources.some(source => ["partial", "blocked", "failed", "interrupted", "not_started", "queued", "running", "waiting_verification"].includes(source.status));
    const skipped = sources.filter(source => source.status === "unsupported").length;
    const updates = sources.map(source => Date.parse(source.updated_at)).filter(Number.isFinite);
    const latest = Math.max(started, ...updates);
    const ended = sameRun && Date.parse(status.finished_at) || latest;
    const age = Math.max(0, (now - latest) / 1000);
    const disconnected = Boolean(options.statusReadFailed || options.readError
      || (running && options.syncedAt && now - options.syncedAt > 20000));
    let label = running ? "正在采集" : incomplete ? "查询未完成" : skipped ? "查询结束 · 有跳过" : "查询结束";
    let tone = running ? "running" : incomplete || skipped ? "warning" : "complete";
    let message = "";
    if (disconnected) {
      label = "进度连接异常";
      tone = "warning";
      message = "暂时无法更新进度，正在自动重试。这里保留最后收到的数据，不能据此判断任务已停止。";
    } else if (running && activeIndex >= 0 && sources[activeIndex].status === "waiting_verification") {
      label = "等待人工验证";
      tone = "warning";
      message = sources[activeIndex].message || "请在来源浏览器完成验证码；完成后自动继续，已有结果可导出。";
    } else if (running && age >= 60) {
      label = "等待新进展";
      tone = "warning";
      message = `已 ${formatProgressDuration(age)} 没有新增进度，可能正在等待源站响应或重试；可查看运行日志。`;
      if (query.criteria?.mode === "ai_recall") {
        message = `已 ${formatProgressDuration(age)} 没有新增进度，可能正在等待源站响应或 AI 复核；可查看运行日志。`;
      }
    }
    const sum = key => sources.reduce((total, source) => total + Math.max(0, Number(source[key]) || 0), 0);
    return {
      running, activeIndex, processed, total: sources.length, skipped, label, tone, message,
      current: running
        ? (activeIndex >= 0 ? `第 ${activeIndex + 1} / ${sources.length} 个来源：${sources[activeIndex].source}` : "正在准备或整理查询结果…")
        : incomplete ? "部分来源未完成，请查看下方状态及原因。" : "本轮来源处理已结束，可以查看或导出查询结果。",
      elapsed: formatProgressDuration(((running ? now : ended) - started) / 1000),
      updated: updates.length
        ? (running ? `最近进展：${formatProgressDuration(age)}前` : `最后进展：${formatLogTimestamp(new Date(latest).toISOString())}`)
        : "等待第一条来源进度",
      notices: sum("notices"), details: sum("details_ok"), links: sum("attachments_found"),
    };
  }

  function renderQueryProgress() {
    if (!elements.queryProgress) return;
    const model = queryProgressModel(activeQuery, querySources, lastStatus, Date.now(), {
      launching: queryLaunching && activeQuery?.id === launchingQueryId, readError: queryProgressReadError,
      statusReadFailed, syncedAt: queryProgressSyncedAt,
    });
    elements.queryProgress.hidden = !model;
    elements.queryProgressNav.hidden = !model;
    if (!model) return;
    elements.queryProgress.dataset.state = model.tone;
    if (elements.queryProgressCurrent.textContent !== model.current) elements.queryProgressCurrent.textContent = model.current;
    elements.queryProgressState.textContent = model.label;
    elements.queryProgressElapsed.textContent = `已用时 ${model.elapsed}`;
    elements.queryProgressUpdated.textContent = model.updated;
    elements.queryProgressBar.max = Math.max(1, model.total);
    elements.queryProgressBar.value = model.processed;
    elements.queryProgressCount.textContent = `已处理 ${model.processed} / ${model.total} 个来源`
      + (model.skipped ? `（${model.skipped} 个未单独查询）` : "");
    const aiRecall = activeQuery?.criteria?.mode === "ai_recall";
    const review = queryReviewCounts(noticeCatalog);
    if (elements.queryProgressMatchesLabel) elements.queryProgressMatchesLabel.textContent = aiRecall ? "AI 判断匹配" : "当前显示";
    if (elements.queryProgressReview) {
      elements.queryProgressReview.hidden = !aiRecall;
      elements.queryProgressReview.textContent = `AI 判断匹配 ${review.matched} 条 · 待人工复核 ${review.pending} 条 · AI 判断不匹配 ${review.rejected} 条。当前列表显示 ${filteredNoticeCatalog.length} 条。`
        + (review.pending ? "待复核记录尚未确认满足客户条件，请核对后再交付。" : "");
    }
    for (const [key, value] of Object.entries({Notices: model.notices, Details: model.details, Matches: aiRecall ? review.matched : filteredNoticeCatalog.length, Links: model.links})) {
      elements[`queryProgress${key}`].textContent = String(value);
    }
    elements.queryProgressWait.hidden = !model.message;
    elements.queryProgressWait.textContent = model.message;
  }

  function renderQueryStatus() {
    const names = {queued: "等待查询", running: "正在查询", waiting_verification: "等待人工验证", ok: "查询结束", empty: "查询返回 0 条",
      partial: "部分完成", blocked: "访问受阻", failed: "查询失败", unsupported: "未单独查询",
      not_started: "未执行", interrupted: "查询中断"};
    const expandedSourceNotes = new Set(Array.from(
      elements.querySources.querySelectorAll("details[open]"), note => note.dataset.source,
    ));
    elements.querySources.replaceChildren(...querySources.map((source, index) => {
      const card = document.createElement("article");
      const state = source.status || "queued";
      card.className = `collection-source is-${Object.hasOwn(names, state) ? state : "queued"}`;
      const heading = document.createElement("div");
      heading.className = "collection-source-heading";
      const title = document.createElement("strong");
      title.textContent = `${index + 1}. ${displaySourceName(source.source)}`;
      const badge = document.createElement("span");
      badge.textContent = names[state] || state;
      heading.append(title, badge);
      const metrics = document.createElement("p");
      metrics.textContent = state === "queued" ? "按来源顺序执行，轮到此来源时自动开始。"
        : state === "not_started" ? "本轮未执行此来源。"
        : state === "unsupported" ? "此入口未单独执行条件查询。"
        : `检索 ${source.notices || 0} 条 · 已读正文 ${source.details_ok || 0} 条 · 附件链接 ${source.attachments_found || 0} 个 · ${source.pages > 0 || state !== "running" ? `列表 ${source.pages || 0} 页` : "页数统计中"}`;
      card.append(heading, metrics);
      if (source.updated_at) {
        const updated = document.createElement("small");
        updated.textContent = `最近更新 ${formatLogTimestamp(source.updated_at)}`;
        card.append(updated);
      }
      if (source.message) {
        const note = document.createElement("details");
        note.className = "collection-source-note";
        note.dataset.source = source.source;
        note.open = expandedSourceNotes.has(source.source);
        const summary = document.createElement("summary");
        summary.textContent = {
          partial: "查看未完成说明", failed: "查看失败原因",
          blocked: "查看受阻原因", waiting_verification: "查看验证要求",
        }[state] || "查看平台说明";
        const message = document.createElement("p");
        message.className = "collection-source-message";
        message.textContent = source.message;
        note.append(summary, message);
        card.append(note);
      }
      return card;
    }));
    renderQueryProgress();
    if (!activeQuery) {
      elements.querySummary.textContent = "尚未发起平台查询。填写条件后点击“查询各平台”。";
      return;
    }
    const q = activeQuery.criteria;
    const queryRegion = comparableQueryCriteria(q);
    const regionLabel = [queryRegion.province ?? "吉林省", queryRegion.city, queryRegion.district].filter(Boolean).join(" / ") || "全国";
    const description = `${q.start_date} 至 ${q.end_date} · ${regionLabel} · ${q.industry || "全部行业"} · ${q.keyword || "不限项目关键词"}`;
    elements.querySummary.textContent = (queryConditionsChanged() ? "条件已修改，请点击“查询各平台”获取新结果。上次条件：" : "本次查询：") + description
      + `。检索词：${activeQuery.terms.filter(Boolean).join("、") || "不限"}。查询时间：${activeQuery.started_at || ""}`;
    if (querySources.some(x => ["partial", "blocked", "failed", "interrupted", "not_started"].includes(x.status))) {
      elements.querySummary.textContent += " 部分平台未完成，当前结果不能代表全部平台。";
    }
  }

  async function runPlatformQuery(button) {
    if (queryLaunching || lastStatus?.running) return;
    if (elements.queryLaunchStatus) elements.queryLaunchStatus.hidden = true;
    queryLaunching = true;
    setButtonsBusy([button], true);
    try {
      if (elements.catalogProvinceFilter && !regionsReady) throw new Error("地区选项尚未加载，请刷新页面后重试。");
      const criteria = readQueryCriteria();
      if (!criteria.start_date || !criteria.end_date || criteria.start_date > criteria.end_date) throw new Error("请填写有效的开始和结束日期。");
      if (criteria.mode === "ai_recall" && (!Number.isInteger(criteria.max_candidates)
          || criteria.max_candidates < 10 || criteria.max_candidates > 500)) {
        throw new Error("每来源处理上限请填写 10 到 500 之间的整数。");
      }
      const saved = await saveConfiguration({silent: true});
      if (!saved) return;
      const selectedProvince = regionCatalog.find(p => p.name === criteria.province);
      const loginSources = selectedProvince?.subdivisions_unavailable ? []
        : (configState.sources || []).filter(source => source.enabled !== false && source.adapter === "okcis");
      if (loginSources.length && !await ensureBrowserLoginSessions(loginSources)) return;
      const response = await apiRequest("/api/query", {method: "POST", body: {criteria,
        ...(criteria.mode === "ai_recall" ? {api_key: elements.apiKey.value.trim()} : {})}});
      if (criteria.mode === "ai_recall") elements.apiKey.value = "";
      activeQuery = response.query;
      launchingQueryId = activeQuery.id;
      queryProgressReadError = "";
      queryProgressSyncedAt = Date.now();
      elements.catalogExportStatus.hidden = true;
      queryRestored = true;
      if (elements.queryReviewFilter) elements.queryReviewFilter.value = "keep";
      noticeCatalog = [];
      selectedNoticeIdentities.clear();
      expandedNoticeIdentities.clear();
      querySources = activeQuery.sources.map(source => ({source, status: "queued", notices: 0}));
      catalogPage = 1;
      elements.catalogAiFilter.value = "all";
      elements.catalogDownloadFilter.value = "";
      renderNoticeCatalog();
      elements.queryProgress.scrollIntoView({behavior: "smooth", block: "start"});
      showToast("已开始查询各平台", "结果会陆续显示；每个平台的查询状态列在下方。", "info");
      await refreshStatus({silent: true});
    } catch(error) {
      const message = humanError(error).replaceAll("ai.endpoint", "模型接口地址")
        .replaceAll("ai.model", "模型名称").replaceAll("ai.api_key_env", "密钥环境变量名");
      if (elements.queryLaunchStatus) {
        elements.queryLaunchStatus.hidden = false;
        elements.queryLaunchStatus.textContent = `未能开始查询：${message}`;
      }
      showToast("未能开始查询", message, "error", 8000);
    } finally {
      queryLaunching = false;
      launchingQueryId = "";
      setButtonsBusy([button], false);
      elements.queryPlatforms.disabled = isOperationActive(lastStatus)
        || Object.keys(lastStatus?.active_downloads || {}).length > 0
        || pendingDownloadIdentities.size > 0
        || Boolean(elements.catalogProvinceFilter && !regionsReady);
    }
  }

  async function loadNoticeCatalog(options = {}) {
    if (catalogLoading) return false;
    catalogLoading = true;
    if (elements.catalogRefresh) setButtonsBusy([elements.catalogRefresh], true);
    if (noticeCatalog.length === 0) {
      setCatalogState("loading", "正在读取标讯清单", "只读取信息，不会下载源文件。");
    }
    try {
      const queryState = await apiRequest("/api/query");
      queryProgressSyncedAt = Date.now();
      queryProgressReadError = "";
      activeQuery = queryState.query;
      querySources = queryState.sources || [];
      if (!queryRestored && activeQuery && configLoaded) {
        queryRestored = true;
        const q = activeQuery.criteria;
        elements.queryMode.value = q.mode || "standard";
        elements.queryLimit.value = q.max_candidates || 100;
        elements.startDate.value = q.start_date;
        elements.endDate.value = q.end_date;
        populateCatalogFilterOptions();
        elements.catalogSearch.value = q.keyword;
        if (elements.catalogProvinceFilter) {
          elements.catalogProvinceFilter.value = q.province ?? "吉林省";
          renderRegionSelectors();
          const province = regionCatalog.find(p => p.name === elements.catalogProvinceFilter.value);
          elements.catalogCityFilter.value = canonicalCatalogCity(q.city, province);
          renderRegionSelectors();
          elements.catalogDistrictFilter.value = q.district || "";
          renderRegionSelectors();
        } else {
          elements.catalogCityFilter.value = q.city;
        }
        elements.catalogIndustryFilter.value = q.industry;
        elements.catalogTypeFilter.value = q.notice_type;
      }
      const queryId = activeQuery?.id;
      const items = queryId ? await loadAllNoticePages(queryId) : [];
      if (queryId !== activeQuery?.id) return false;
      noticeCatalog = items
        .map((item, index) => normaliseNotice(item, index))
        .filter((item) => item.identity || item.title || item.officialUrl)
        .sort(compareNoticesNewestFirst);
      preservePendingDownloads(noticeCatalog, pendingDownloadIdentities);
      const existingIdentities = new Set(noticeCatalog.map((item) => item.identity).filter(Boolean));
      selectedNoticeIdentities = new Set(
        Array.from(selectedNoticeIdentities).filter((identity) => existingIdentities.has(identity)),
      );
      expandedNoticeIdentities = new Set(
        Array.from(expandedNoticeIdentities).filter((identity) => existingIdentities.has(identity)),
      );
      populateCatalogFilterOptions();
      renderNoticeCatalog();
      return true;
    } catch (error) {
      queryProgressReadError = humanError(error);
      renderQueryProgress();
      setCatalogState(
        "error",
        "标讯清单暂时无法读取",
        `${humanError(error)}。采集配置和已有文件不会受到影响。`,
      );
      if (!options.silent) showToast("清单刷新失败", humanError(error), "error", 7000);
      return false;
    } finally {
      catalogLoading = false;
      if (elements.catalogRefresh) setButtonsBusy([elements.catalogRefresh], false);
    }
  }

  async function loadAllNoticePages(queryId) {
    const baseQuery = `page_size=200&relevance=all&query_id=${encodeURIComponent(queryId)}`;
    const first = await apiRequest(`/api/notices?page=1&${baseQuery}`);
    const items = extractNoticeItems(first).slice();
    const pages = Math.max(1, Math.min(1000, Number(first?.pages) || 1));
    // The data is local SQLite, so fetching bounded batches keeps a large
    // catalogue responsive without issuing hundreds of requests at once.
    for (let start = 2; start <= pages; start += 6) {
      const requests = [];
      for (let page = start; page < Math.min(start + 6, pages + 1); page += 1) {
        requests.push(apiRequest(`/api/notices?page=${page}&${baseQuery}`));
      }
      const responses = await Promise.all(requests);
      for (const response of responses) items.push(...extractNoticeItems(response));
    }
    return items;
  }

  function extractNoticeItems(response) {
    if (Array.isArray(response)) return response;
    if (!isPlainObject(response)) return [];
    for (const key of ["notices", "items", "results", "records"]) {
      if (Array.isArray(response[key])) return response[key];
    }
    if (isPlainObject(response.data)) return extractNoticeItems(response.data);
    return [];
  }

  function normaliseNotice(rawValue, index = 0) {
    const raw = isPlainObject(rawValue) ? rawValue : {};
    const ai = isPlainObject(raw.ai) ? raw.ai : {};
    const download = isPlainObject(raw.download) ? raw.download : {};
    const identity = stringValue(firstNoticeValue(raw, [
      "identity", "notice_identity", "notice_id", "record_id", "external_id", "id",
    ]));
    const officialUrl = stringValue(firstNoticeValue(raw, [
      "official_notice_url", "official_url", "original_notice_url", "source_url", "announcement_url", "url",
    ]));
    const tags = normaliseTags(firstNoticeValue(raw, ["project_tags", "tags", "tag"]));
    const aiCategory = stringValue(firstNoticeValue(raw, [
      "cybersecurity_category", "ai_category", "security_category", "project_category",
    ]) || firstNoticeValue(ai, ["category", "classification", "label"]));
    const aiDecision = normaliseAiDecision(
      firstNoticeValue(raw, ["ai_decision", "ai_status", "relevance_status", "decision"]),
      firstNoticeValue(ai, ["decision", "status", "relevance"]),
      raw.is_relevant,
    );
    const status = normaliseDownloadStatus(
      firstNoticeValue(raw, ["download_status", "file_status", "artifact_status", "source_file_status"])
        || firstNoticeValue(download, ["status", "state"]),
    );
    const attachmentCountValue = firstNoticeValue(raw, ["attachment_count", "attachments_count"]);
    const attachments = Array.isArray(raw.attachments) ? raw.attachments : [];
    const attachmentCount = Number.isFinite(Number(attachmentCountValue))
      ? Number(attachmentCountValue)
      : attachments.length;

    return {
      raw,
      identity,
      domIdentity: identity || `catalog-row-${index}`,
      title: stringValue(firstNoticeValue(raw, ["title", "data_title", "notice_title", "project_name"])),
      noticeType: stringValue(firstNoticeValue(raw, [
        "notice_type", "announcement_type", "notice_category", "bulletin_type", "category",
      ])),
      purchaser: stringValue(firstNoticeValue(raw, [
        "purchaser_name", "buyer_name", "buyer", "client_name", "customer_name", "procuring_entity",
      ])),
      industry: stringValue(firstNoticeValue(raw, [
        "industry", "industry_label", "primary_industry", "level_one_industry",
      ])),
      city: stringValue(firstNoticeValue(raw, ["city", "city_name", "region_city", "area"])),
      location: isPlainObject(raw.location) ? raw.location : {},
      purchaserContact: stringValue(firstNoticeValue(raw, [
        "purchaser_contact", "buyer_contact", "customer_contact", "contact_name",
      ])),
      purchaserPhone: stringValue(firstNoticeValue(raw, [
        "purchaser_phone", "buyer_phone", "customer_phone", "contact_phone",
      ])),
      vendor: stringValue(firstNoticeValue(raw, [
        "winner_name", "winning_vendor", "award_vendor", "supplier_name", "vendor_name",
      ])),
      vendorContact: stringValue(firstNoticeValue(raw, [
        "winner_contact", "winning_vendor_contact", "supplier_contact", "vendor_contact",
      ])),
      vendorPhone: stringValue(firstNoticeValue(raw, [
        "winner_phone", "winning_vendor_phone", "supplier_phone", "vendor_phone",
      ])),
      publishedAt: stringValue(firstNoticeValue(raw, [
        "published_at", "publish_date", "announcement_date", "notice_date", "date",
      ])),
      tenderAmount: explicitAmount(raw, ["tender_amount", "budget_amount", "procurement_budget"], [
        "budget_amount_minor", "max_price_minor", "intention_amount_minor",
      ]),
      awardAmount: explicitAmount(raw, ["award_amount", "winning_amount", "deal_amount", "contract_amount"], [
        "contract_amount_minor", "award_amount_minor",
      ]),
      amount: explicitAmount(raw, ["amount", "project_amount"], ["amount_minor"]),
      projectInfo: stringValue(firstNoticeValue(raw, [
        "project_info", "project_summary", "summary", "description",
      ])),
      projectCode: stringValue(firstNoticeValue(raw, ["project_code", "project_number", "notice_code"])),
      tags,
      sourceName: stringValue(firstNoticeValue(raw, ["source_name", "source", "source_id"])),
      officialUrl,
      aiCategory,
      aiDecision,
      aiConfidence: firstNoticeValue(raw, ["ai_confidence", "confidence"])
        ?? firstNoticeValue(ai, ["confidence", "score"]),
      aiReason: stringValue(firstNoticeValue(raw, ["ai_reason", "classification_reason"])
        || firstNoticeValue(ai, ["reason", "explanation"])),
      queryReview: isPlainObject(raw.query_review) ? raw.query_review : {},
      downloadStatus: status,
      downloadError: stringValue(firstNoticeValue(raw, ["download_error", "file_error"])
        || firstNoticeValue(download, ["error", "message"])),
      attachmentCount,
    };
  }

  function firstNoticeValue(object, keys) {
    if (!isPlainObject(object)) return undefined;
    for (const key of keys) {
      const value = object[key];
      if (value !== null && value !== undefined && value !== "") return value;
    }
    return undefined;
  }

  function stringValue(value) {
    if (value === null || value === undefined) return "";
    if (Array.isArray(value)) return value.map(stringValue).filter(Boolean).join("、");
    if (isPlainObject(value)) return "";
    return String(value).trim();
  }

  function explicitAmount(object, directKeys, minorKeys) {
    const direct = firstNoticeValue(object, directKeys);
    if (direct !== undefined) return direct;
    const minor = firstNoticeValue(object, minorKeys);
    if (minor === undefined) return undefined;
    const number = Number(minor);
    return Number.isFinite(number) ? number / 100 : minor;
  }

  function normaliseTags(value) {
    if (Array.isArray(value)) return value.map(stringValue).filter(Boolean);
    const text = stringValue(value);
    if (!text) return [];
    return text.split(/[|,，;；、]/).map((item) => item.trim()).filter(Boolean);
  }

  function normaliseAiDecision(...values) {
    const booleanValue = values.find((value) => typeof value === "boolean");
    if (booleanValue === true) return "relevant";
    if (booleanValue === false) return "excluded";
    const text = values.map(stringValue).find(Boolean)?.toLowerCase() || "";
    if (["excluded", "exclude", "irrelevant", "rejected", "reject", "no", "false", "排除", "不相关"].some((token) => text.includes(token))) {
      return "excluded";
    }
    if (["relevant", "confirmed", "accepted", "accept", "related", "yes", "true", "相关", "已确认"].some((token) => text.includes(token))) {
      return "relevant";
    }
    return "review";
  }

  function normaliseDownloadStatus(value) {
    const text = stringValue(value).toLowerCase();
    if (["queued", "pending", "requested", "waiting", "等待下载"].includes(text)) return "queued";
    if (["downloading", "running", "fetching", "下载中"].includes(text)) return "downloading";
    if (["downloaded", "complete", "completed", "ready", "success", "delivered", "已下载"].includes(text)) return "downloaded";
    if (["partial", "partially_downloaded", "部分下载"].includes(text)) return "partial";
    if (["failed", "error", "download_failed", "下载失败"].includes(text)) return "failed";
    if (["unavailable", "missing", "not_available", "no_source", "暂无原文件"].includes(text)) return "unavailable";
    return "not_downloaded";
  }

  function compareNoticesNewestFirst(left, right) {
    const leftTime = Date.parse(left.publishedAt) || 0;
    const rightTime = Date.parse(right.publishedAt) || 0;
    if (leftTime !== rightTime) return rightTime - leftTime;
    return left.title.localeCompare(right.title, "zh-CN");
  }

  function populateCatalogFilterOptions() {
    updateCatalogSelect(
      elements.catalogTypeFilter,
      noticeCatalog.map((item) => item.noticeType),
      "全部类别",
      ["采购公告", "中标公告", "成交公告", "更正公告", "废标公告", "合同公告", "采购意向"],
    );
    if (elements.catalogProvinceFilter) renderRegionSelectors();
    else updateCatalogSelect(elements.catalogCityFilter, noticeCatalog.map(item => item.city), "全部城市", CATALOG_CITIES);
    updateCatalogSelect(
      elements.catalogIndustryFilter,
      noticeCatalog.map((item) => item.industry).filter(value => CATALOG_INDUSTRIES.includes(value)),
      "全部行业",
      CATALOG_INDUSTRIES,
    );
  }

  function catalogFilterChoices(values, defaults = []) {
    const cleaned = values.map(stringValue);
    const unique = Array.from(new Set([...defaults, ...cleaned.filter(Boolean)]));
    if (!defaults.length) unique.sort((a,b)=>a.localeCompare(b,"zh-CN"));
    return [...unique, "__unknown__"].map(value=>({
      value,
      label: value === "__unknown__" ? "未标明" : value === "吉林省" ? "未明确城市（省级数据）" : value,
      count: cleaned.filter(item=>value === "__unknown__" ? !item : item === value).length,
    }));
  }

  function updateCatalogSelect(select, values, allLabel, defaults = []) {
    const selected = select.value;
    const choices = catalogFilterChoices(values, defaults);
    const signature = JSON.stringify(choices.map(item => [item.value, item.label]));
    // Polling must not rebuild a native select while the user is choosing.
    if (select.dataset.optionsSignature === signature || document.activeElement === select) return;
    select.replaceChildren(createOption("", allLabel), ...choices.filter(item => item.value !== "__unknown__").map(item => createOption(item.value, item.label)));
    select.value = choices.some(item=>item.value === selected) ? selected : "";
    select.dataset.optionsSignature = signature;
  }

  function queryReviewCounts(rows) {
    const matched = rows.filter(n => n.queryReview?.status === "match").length;
    const rejected = rows.filter(n => n.queryReview?.status === "no_match").length;
    return {matched, rejected, pending: rows.length - matched - rejected};
  }

  function matchesSafetyFilter(notice, filter) {
    if (filter === "relevant_pending") return notice.aiDecision !== "excluded";
    return !["relevant", "review", "excluded"].includes(filter) || notice.aiDecision === filter;
  }

  function renderNoticeCatalog() {
    renderQueryMode();
    const query = elements.catalogSearch.value.trim().toLocaleLowerCase("zh-CN");
    const type = elements.catalogTypeFilter.value;
    const city = elements.catalogCityFilter.value;
    const industry = elements.catalogIndustryFilter.value;
    const aiFilter = elements.catalogAiFilter.value || "relevant_pending";
    const downloadStatus = elements.catalogDownloadFilter.value;

    const conditionsChanged = queryConditionsChanged();
    const aiRecall = activeQuery?.criteria?.mode === "ai_recall";
    filteredNoticeCatalog = noticeCatalog.filter((notice) => {
      if (conditionsChanged) return false;
      if (!matchesSafetyFilter(notice, aiFilter)) return false;
      if (aiRecall) {
        // Customer criteria have already been reviewed semantically. Repeating
        // exact city/industry/keyword filters here would discard rescued rows.
        return queryReviewVisible(notice.queryReview, elements.queryReviewFilter.value)
          && (!downloadStatus || notice.downloadStatus === downloadStatus);
      }
      const haystack = [
        notice.title,
        notice.purchaser,
        notice.vendor,
        notice.industry,
        notice.projectCode,
        notice.tags.join(" "),
      ].join(" ").toLocaleLowerCase("zh-CN");
      if (query && !haystack.includes(query)) return false;
      if (type && (type === "__unknown__" ? Boolean(notice.noticeType) : notice.noticeType !== type)) return false;
      if (elements.catalogProvinceFilter) {
        if (!noticeMatchesRegion(notice)) return false;
      } else if (city && (city === "__unknown__" ? Boolean(notice.city) : notice.city !== city)) return false;
      if (industry && (industry === "__unknown__" ? Boolean(notice.industry) : notice.industry !== industry)) return false;
      if (downloadStatus && notice.downloadStatus !== downloadStatus) return false;
      return true;
    });

    const totalPages = Math.max(1, Math.ceil(filteredNoticeCatalog.length / catalogPageSize));
    catalogPage = Math.min(Math.max(1, catalogPage), totalPages);
    const pageStart = (catalogPage - 1) * catalogPageSize;
    const pageNotices = currentCatalogPageNotices();
    const excludedCount = noticeCatalog.filter((notice) => notice.aiDecision === "excluded").length;
    renderQueryStatus();
    // A selection hidden by a changed result filter must not leak into exports.
    const visibleIds = new Set(filteredNoticeCatalog.map(notice => notice.identity));
    selectedNoticeIdentities = new Set([...selectedNoticeIdentities].filter(id => visibleIds.has(id)));
    elements.catalogSummary.textContent = noticeCatalog.length === filteredNoticeCatalog.length
      ? `共 ${noticeCatalog.length} 条信息`
      : `显示 ${filteredNoticeCatalog.length} / ${noticeCatalog.length} 条信息`;
    if (aiFilter === "relevant_pending" && excludedCount > 0) {
      elements.catalogSummary.textContent += ` · ${excludedCount} 条已排除信息可在“AI 结果”中审计`;
    }

    elements.catalogBody.replaceChildren(...pageNotices.flatMap(createNoticeRows));
    elements.catalogTableWrap.hidden = pageNotices.length === 0;
    elements.catalogEmpty.hidden = pageNotices.length !== 0;
    elements.catalogPagination.hidden = filteredNoticeCatalog.length === 0;
    elements.catalogPageLabel.textContent = `第 ${catalogPage} / ${totalPages} 页 · ${pageStart + 1}–${pageStart + pageNotices.length} 条`;
    elements.catalogPrevPage.disabled = catalogPage <= 1;
    elements.catalogNextPage.disabled = catalogPage >= totalPages;
    elements.catalogState.hidden = true;

    if (pageNotices.length === 0) {
    const hasAnyFilter = Boolean(query || type || city || elements.catalogProvinceFilter?.value || elements.catalogDistrictFilter?.value || industry || downloadStatus || aiFilter !== "relevant_pending");
      elements.catalogEmptyMessage.textContent = noticeCatalog.length === 0
        ? (activeQuery ? "本次暂未取得结果。请查看上方各平台的查询状态；查询受阻不等于没有项目。" : "请先填写客户要求，然后点击“查询各平台”。")
        : hasAnyFilter
          ? (conditionsChanged ? "条件已修改，请点击“查询各平台”，重新向平台取数据。" : "本次已取得的公告中没有符合这些条件的结果，请查看各平台状态，或调整条件重新查询。")
          : "当前没有 AI 确认相关或待复核的信息；可切换到“全部（审计）”查看已排除记录。";
    }
    updateCatalogSelectionPresentation(pageNotices);
  }

  function createNoticeRows(notice) {
    const row = document.createElement("tr");
    row.className = "catalog-row";
    row.dataset.noticeIdentity = notice.identity;
    row.classList.toggle("is-selected", selectedNoticeIdentities.has(notice.identity));

    const checkCell = appendCatalogCell(row, "选择", "catalog-check-cell");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.catalogSelect = notice.identity;
    checkbox.setAttribute("aria-label", `选择：${displayOrDash(notice.title)}`);
    checkbox.checked = selectedNoticeIdentities.has(notice.identity);
    checkbox.disabled = !isNoticeSelectable(notice);
    checkCell.append(checkbox);

    const dateCell = appendCatalogCell(row, "公告时间 / 类别", "catalog-date-cell");
    dateCell.append(
      createCatalogElement("strong", "", formatCatalogDate(notice.publishedAt)),
      createCatalogElement("span", "catalog-muted", displayOrDash(notice.noticeType)),
    );

    const titleCell = appendCatalogCell(row, "项目标题", "catalog-title-cell");
    titleCell.append(createCatalogElement("strong", "catalog-project-title", displayOrDash(notice.title)));
    if (notice.projectCode) titleCell.append(createCatalogElement("small", "catalog-muted catalog-preview-text", `项目编号：${notice.projectCode}`));

    const purchaserCell = appendCatalogCell(row, "采购单位 / 城市");
    purchaserCell.append(
      createCatalogElement("strong", "catalog-preview-text", displayOrDash(notice.purchaser)),
      createCatalogElement("span", "catalog-muted", displayOrDash(notice.city)),
    );

    appendCatalogCell(row, "中标单位").append(
      createCatalogElement("span", "catalog-preview-text", displayOrDash(notice.vendor)),
    );

    const amountCell = appendCatalogCell(row, "金额", "catalog-amount-cell");
    const amount = primaryNoticeAmount(notice);
    amountCell.append(
      createCatalogElement("strong", "", amount.value),
      createCatalogElement("span", "catalog-muted", amount.label),
    );

    const classificationCell = appendCatalogCell(row, "行业 / AI 分类");
    classificationCell.append(createCatalogElement("span", "", displayOrDash(notice.industry)));
    const decision = aiDecisionPresentation(notice.aiDecision);
    classificationCell.append(createCatalogElement("span", `catalog-chip ${decision.className}`, decision.label));
    if (notice.aiCategory) classificationCell.append(createCatalogElement("small", "catalog-muted", notice.aiCategory));
    if (activeQuery?.criteria?.mode === "ai_recall") {
      const review = queryReviewPresentation(notice.queryReview);
      classificationCell.append(createCatalogElement("span", `catalog-chip ${review.className}`, review.label));
      classificationCell.append(createCatalogElement("small", "catalog-muted catalog-preview-text", notice.queryReview.reason || "尚未完成复核"));
    }

    const sourceCell = appendCatalogCell(row, "来源 / 文件状态");
    sourceCell.append(createCatalogElement("span", "catalog-preview-text", displayOrDash(displaySourceName(notice.sourceName))));
    const fileState = downloadStatusPresentation(notice.downloadStatus);
    sourceCell.append(createCatalogElement("span", `catalog-chip ${fileState.className}`, fileState.label));

    const actionsCell = appendCatalogCell(row, "操作", "catalog-actions-cell");
    const actionGroup = createCatalogElement("div", "catalog-row-actions");
    if (safeCatalogUrl(notice.officialUrl)) {
      const sourceLink = createCatalogElement("a", "catalog-action-link", "查看原公告");
      sourceLink.href = safeCatalogUrl(notice.officialUrl);
      sourceLink.target = "_blank";
      sourceLink.rel = "noopener noreferrer";
      actionGroup.append(sourceLink);
    } else {
      actionGroup.append(createCatalogElement("span", "catalog-action-link is-disabled", "暂无原链接"));
    }
    const detailsButton = createCatalogElement("button", "catalog-action-link", expandedNoticeIdentities.has(notice.domIdentity) ? "收起详情" : "展开详情");
    detailsButton.type = "button";
    detailsButton.dataset.catalogAction = "details";
    detailsButton.dataset.identity = notice.domIdentity;
    detailsButton.setAttribute("aria-expanded", String(expandedNoticeIdentities.has(notice.domIdentity)));
    actionGroup.append(detailsButton);

    const downloadButton = createCatalogElement("button", "button button-secondary button-small catalog-download-button", downloadButtonLabel(notice));
    downloadButton.type = "button";
    downloadButton.dataset.catalogAction = "download";
    downloadButton.dataset.identity = notice.identity;
    downloadButton.disabled = !isNoticeDownloadable(notice);
    actionGroup.append(downloadButton);
    actionsCell.append(actionGroup);

    if (!expandedNoticeIdentities.has(notice.domIdentity)) return [row];
    return [row, createNoticeDetailsRow(notice)];
  }

  function createNoticeDetailsRow(notice) {
    const row = document.createElement("tr");
    row.className = "catalog-details-row";
    const cell = document.createElement("td");
    cell.colSpan = 9;
    const details = createCatalogElement("dl", "catalog-details-grid");
    appendCatalogDetail(details, "项目标题", notice.title, "catalog-detail-wide");
    appendCatalogDetail(details, "公告类别", notice.noticeType);
    appendCatalogDetail(details, "客户名称", notice.purchaser);
    appendCatalogDetail(details, "一级行业标签", notice.industry);
    appendCatalogDetail(details, "城市", notice.city);
    if (notice.location?.province) appendCatalogDetail(details, "项目地区", [notice.location.province, notice.location.city, notice.location.district].filter(Boolean).join(" / "));
    appendCatalogDetail(details, "客户联系人", notice.purchaserContact);
    appendCatalogDetail(details, "客户电话", notice.purchaserPhone);
    appendCatalogDetail(details, "中标单位", notice.vendor);
    appendCatalogDetail(details, "中标单位联系人", notice.vendorContact);
    appendCatalogDetail(details, "中标单位电话", notice.vendorPhone);
    appendCatalogDetail(details, "项目标签", notice.tags.join("、"));
    appendCatalogDetail(details, "招标 / 预算金额", formatCatalogMoney(notice.tenderAmount));
    appendCatalogDetail(details, "中标 / 合同金额", formatCatalogMoney(notice.awardAmount));
    appendCatalogDetail(details, "附件索引", notice.attachmentCount ? `${notice.attachmentCount} 个` : "尚无附件直链，请查看原公告的文件获取方式");
    appendCatalogDetail(details, "AI 置信度", formatAiConfidence(notice.aiConfidence));
    appendCatalogDetail(details, "AI 判断说明", notice.aiReason, "catalog-detail-wide");
    if (activeQuery?.criteria?.mode === "ai_recall") {
      appendCatalogDetail(details, "AI 客户需求复核", queryReviewPresentation(notice.queryReview).label);
      appendCatalogDetail(details, "需求复核说明", notice.queryReview.reason, "catalog-detail-wide");
      appendCatalogDetail(details, "AI 逐字原文依据", (notice.queryReview.evidence || []).join("；"), "catalog-detail-wide");
      appendCatalogDetail(details, "文件线索缺口", (notice.queryReview.issues || []).join("；"), "catalog-detail-wide");
    }
    appendCatalogDetail(details, "项目信息", notice.projectInfo, "catalog-detail-wide");
    const linkItem = appendCatalogDetail(details, "公告页面", "", "catalog-detail-wide");
    const safeUrl = safeCatalogUrl(notice.officialUrl);
    if (safeUrl) {
      const link = createCatalogElement("a", "catalog-detail-link", safeUrl);
      link.href = safeUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      linkItem.querySelector("dd").replaceChildren(link);
    }
    if (notice.downloadError) appendCatalogDetail(details, "下载错误", notice.downloadError, "catalog-detail-wide is-error");
    cell.append(details);
    row.append(cell);
    return row;
  }

  function appendCatalogCell(row, label, className = "") {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    cell.dataset.label = label;
    row.append(cell);
    return cell;
  }

  function appendCatalogDetail(list, label, value, className = "") {
    const item = createCatalogElement("div", `catalog-detail-item${className ? ` ${className}` : ""}`);
    item.append(
      createCatalogElement("dt", "", label),
      createCatalogElement("dd", "", displayOrDash(value)),
    );
    list.append(item);
    return item;
  }

  function createCatalogElement(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== null && text !== undefined) element.textContent = String(text);
    if (className.includes("catalog-preview-text") || className === "catalog-project-title") {
      element.title = element.textContent;
    }
    return element;
  }

  function displayOrDash(value) {
    const text = stringValue(value);
    return text || "—";
  }

  function formatCatalogDate(value) {
    const text = stringValue(value);
    if (!text) return "—";
    const match = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
    return match ? `${match[1]}.${match[2]}.${match[3]}` : text;
  }

  function formatCatalogMoney(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "number" && Number.isFinite(value)) {
      return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)} 元`;
    }
    return stringValue(value) || "—";
  }

  function primaryNoticeAmount(notice) {
    if (notice.awardAmount !== null && notice.awardAmount !== undefined && notice.awardAmount !== "") {
      return { value: formatCatalogMoney(notice.awardAmount), label: "中标 / 合同" };
    }
    if (notice.tenderAmount !== null && notice.tenderAmount !== undefined && notice.tenderAmount !== "") {
      return { value: formatCatalogMoney(notice.tenderAmount), label: "招标 / 预算" };
    }
    if (notice.amount !== null && notice.amount !== undefined && notice.amount !== "") {
      return { value: formatCatalogMoney(notice.amount), label: "公告金额" };
    }
    return { value: "—", label: "" };
  }

  function formatAiConfidence(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    if (!Number.isFinite(number)) return stringValue(value) || "—";
    return number <= 1 ? `${Math.round(number * 100)}%` : `${number}%`;
  }

  function aiDecisionPresentation(status) {
    if (status === "relevant") return { label: "AI 确认相关", className: "is-success" };
    if (status === "excluded") return { label: "AI 已排除", className: "is-muted" };
    return { label: "待分类 / 复核", className: "is-warning" };
  }

  function downloadStatusPresentation(status) {
    const presentations = {
      not_downloaded: { label: "未下载", className: "is-neutral" },
      queued: { label: "等待下载", className: "is-warning" },
      downloading: { label: "下载中", className: "is-info" },
      downloaded: { label: "已下载", className: "is-success" },
      failed: { label: "下载失败", className: "is-error" },
      partial: { label: "部分已下载", className: "is-warning" },
      unavailable: { label: "暂无原文件", className: "is-muted" },
    };
    return presentations[status] || presentations.not_downloaded;
  }

  function downloadButtonLabel(notice) {
    if (notice.downloadStatus === "queued") return "等待下载";
    if (notice.downloadStatus === "downloading") return "下载中";
    if (notice.downloadStatus === "downloaded") return "已下载";
    if (notice.downloadStatus === "failed") return "重试下载";
    if (notice.downloadStatus === "partial") return "下载缺失文件";
    if (notice.downloadStatus === "unavailable") return "暂无原文件";
    return "下载原文件";
  }

  function isNoticeSelectable(notice) {
    return Boolean(notice.identity);
  }

  function isNoticeDownloadable(notice) {
    return isNoticeSelectable(notice)
      && !["queued", "downloading", "downloaded", "unavailable"].includes(notice.downloadStatus);
  }

  function safeCatalogUrl(value) {
    const text = stringValue(value);
    if (!text) return "";
    try {
      const url = new URL(text, window.location.origin);
      return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch (_error) {
      return "";
    }
  }

  function handleCatalogClick(event) {
    const button = event.target.closest("[data-catalog-action]");
    if (!button) return;
    const action = button.dataset.catalogAction;
    const identity = String(button.dataset.identity || "");
    if (action === "details") {
      if (expandedNoticeIdentities.has(identity)) expandedNoticeIdentities.delete(identity);
      else expandedNoticeIdentities.add(identity);
      renderNoticeCatalog();
      return;
    }
    if (action === "download" && identity) downloadNoticeFiles([identity], button);
  }

  function handleCatalogChange(event) {
    const checkbox = event.target.closest("[data-catalog-select]");
    if (!checkbox) return;
    const identity = String(checkbox.dataset.catalogSelect || "");
    if (checkbox.checked) selectedNoticeIdentities.add(identity);
    else selectedNoticeIdentities.delete(identity);
    checkbox.closest(".catalog-row")?.classList.toggle("is-selected", checkbox.checked);
    updateCatalogSelectionPresentation(currentCatalogPageNotices());
  }

  function toggleCatalogPageSelection() {
    const selectable = currentCatalogPageNotices().filter(isNoticeSelectable);
    for (const notice of selectable) {
      if (elements.catalogSelectAll.checked) selectedNoticeIdentities.add(notice.identity);
      else selectedNoticeIdentities.delete(notice.identity);
    }
    renderNoticeCatalog();
  }

  function currentCatalogPageNotices() {
    const pageStart = (catalogPage - 1) * catalogPageSize;
    return filteredNoticeCatalog.slice(pageStart, pageStart + catalogPageSize);
  }

  function normaliseCatalogPageSize(value) {
    const size = Number(value);
    return CATALOG_PAGE_SIZES.includes(size) ? size : DEFAULT_CATALOG_PAGE_SIZE;
  }

  function restoreCatalogPageSize() {
    try {
      catalogPageSize = normaliseCatalogPageSize(window.localStorage.getItem(CATALOG_PAGE_SIZE_STORAGE_KEY));
    } catch (_error) {
      catalogPageSize = DEFAULT_CATALOG_PAGE_SIZE;
    }
    elements.catalogPageSize.value = String(catalogPageSize);
  }

  function changeCatalogPageSize(value) {
    catalogPageSize = normaliseCatalogPageSize(value);
    catalogPage = 1;
    elements.catalogPageSize.value = String(catalogPageSize);
    try {
      window.localStorage.setItem(CATALOG_PAGE_SIZE_STORAGE_KEY, String(catalogPageSize));
    } catch (_error) {
      // The preference remains usable when browser storage is unavailable.
    }
    renderNoticeCatalog();
  }

  function updateCatalogSelectionPresentation(pageNotices = currentCatalogPageNotices()) {
    const selectable = pageNotices.filter(isNoticeSelectable);
    const selectedOnPage = selectable.filter((notice) => selectedNoticeIdentities.has(notice.identity));
    elements.catalogSelectAll.checked = selectable.length > 0 && selectedOnPage.length === selectable.length;
    elements.catalogSelectAll.indeterminate = selectedOnPage.length > 0 && selectedOnPage.length < selectable.length;
    elements.catalogSelectAll.disabled = selectable.length === 0;
    elements.catalogSelectionCount.textContent = `已选 ${selectedNoticeIdentities.size} 条`;
    elements.catalogDownloadSelected.disabled = !filteredNoticeCatalog.some(notice =>
      selectedNoticeIdentities.has(notice.identity) && isNoticeDownloadable(notice));
    updateCatalogExportPresentation();
  }

  function catalogExportIdentities() {
    const visible = filteredNoticeCatalog.map(notice => notice.identity);
    return selectedNoticeIdentities.size
      ? visible.filter(id => selectedNoticeIdentities.has(id)) : visible;
  }

  function updateCatalogExportPresentation() {
    const ids = catalogExportIdentities();
    elements.catalogExport.disabled = !activeQuery || !ids.length || queryLaunching
      || catalogExportBusy || queryConditionsChanged();
    const scope = selectedNoticeIdentities.size ? `已勾选的 ${ids.length} 条` : `当前筛选的全部 ${ids.length} 条`;
    elements.catalogExportScope.textContent = ids.length
      ? `将导出${scope}公告，含项目信息、文件名、下载 URL 和获取说明；多个附件分别占一行。`
        + (isOperationActive(lastStatus) ? "平台仍在查询，导出的是当前已取得的结果。" : "")
      : "查询取得结果后可导出 CSV 表格，包含项目信息及文件下载 URL。";
  }

  function resetCatalogFilters() {
    elements.catalogSearch.value = "";
    elements.catalogTypeFilter.value = "";
    elements.catalogCityFilter.value = "";
    if (elements.catalogProvinceFilter) {
      elements.catalogProvinceFilter.value = "";
      elements.catalogDistrictFilter.value = "";
      renderRegionSelectors();
    }
    elements.catalogIndustryFilter.value = "";
    elements.catalogAiFilter.value = "relevant_pending";
    elements.catalogDownloadFilter.value = "";
    catalogPage = 1;
    renderNoticeCatalog();
  }

  function changeCatalogPage(delta) {
    catalogPage += delta;
    renderNoticeCatalog();
    elements.catalogSummary.closest(".catalog-summary-bar")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }

  async function downloadNoticeFiles(identities, button) {
    const unique = Array.from(new Set(identities.map(String).filter(Boolean)));
    const eligible = unique.filter((identity) => {
      const notice = noticeCatalog.find((item) => item.identity === identity);
      return notice && isNoticeDownloadable(notice);
    });
    if (eligible.length === 0) {
      showToast("没有可下载的标讯", "已下载、正在下载或暂无源文件的项目不会重复提交。", "warning");
      return;
    }
    const previousStatuses = new Map();
    for (const notice of noticeCatalog) {
      if (!eligible.includes(notice.identity)) continue;
      previousStatuses.set(notice.identity, notice.downloadStatus);
      pendingDownloadIdentities.set(notice.identity, "queued");
      notice.downloadStatus = "queued";
      selectedNoticeIdentities.delete(notice.identity);
    }
    renderNoticeCatalog();
    setButtonsBusy([button], true);
    const submittedIdentities = new Set();
    try {
      const responses = [];
      for (let start = 0; start < eligible.length; start += 1) {
        const batch = eligible.slice(start, start + 1);
        batch.forEach(identity => pendingDownloadIdentities.set(identity, "downloading"));
        for (const notice of noticeCatalog) {
          if (batch.includes(notice.identity)) notice.downloadStatus = "downloading";
        }
        renderNoticeCatalog();
        const response = await apiRequest("/api/notices/download", {
          method: "POST",
          body: {
            identities: batch,
            include_notice: true,
            include_attachments: true,
            source_credentials: collectSourceCredentials(),
          },
        });
        responses.push(response);
        batch.forEach(identity => pendingDownloadIdentities.delete(identity));
        applyCatalogDownloadResponse(response, batch);
        batch.forEach((identity) => submittedIdentities.add(identity));
        renderNoticeCatalog();
      }
      renderNoticeCatalog();
      const directDownload = responses.map((response) => response?.download_url).find((value) => safeCatalogUrl(value));
      if (directDownload) triggerUrlDownload(directDownload);
      const incomplete = responses.some((response) => response?.ok === false);
      const files = responses.flatMap(response => (response.items || []).flatMap(item => item.files || []));
      const attachmentCount = files.filter(file => file.kind === "attachment").length;
      const noticeCount = files.filter(file => file.kind === "notice").length;
      showToast(
        incomplete
          ? "部分原文件未能下载"
          : `已完成 ${eligible.length} 条下载`,
        incomplete
          ? "已保留成功下载的原文件；可以重试失败项，或点击“导出表格（含下载URL）”查看链接和获取说明。"
          : `已保存 ${noticeCount} 个原公告、${attachmentCount} 个附件。点“打开输出目录”查看。${attachmentCount ? "" : "原公告不等于标书附件，请按公告中的获取方式办理。"}`,
        incomplete ? "warning" : "success",
        6000,
      );
      startCatalogDownloadPolling();
    } catch (error) {
      for (const notice of noticeCatalog) {
        if (!previousStatuses.has(notice.identity)) continue;
        if (submittedIdentities.has(notice.identity)) continue;
        notice.downloadStatus = previousStatuses.get(notice.identity) === "failed" ? "failed" : "not_downloaded";
      }
      renderNoticeCatalog();
      showToast(
        submittedIdentities.size > 0 ? "部分下载批次已提交" : "原文件下载未提交",
        humanError(error),
        submittedIdentities.size > 0 ? "warning" : "error",
        8000,
      );
      if (submittedIdentities.size > 0) startCatalogDownloadPolling();
    } finally {
      eligible.forEach(identity => pendingDownloadIdentities.delete(identity));
      setButtonsBusy([button], false);
      await loadNoticeCatalog({ silent: true });
    }
  }

  function preservePendingDownloads(notices, pending) {
    for (const notice of notices) {
      if (pending.has(notice.identity)) notice.downloadStatus = pending.get(notice.identity);
    }
  }

  function applyCatalogDownloadResponse(response, requestedIdentities) {
    const responseItems = extractNoticeItems(response);
    const updates = new Map(responseItems.map((item, index) => {
      const normalised = normaliseNotice(item, index);
      const explicitStatus = firstNoticeValue(item, ["download_status", "file_status", "status"]);
      if (explicitStatus !== undefined) normalised.downloadStatus = normaliseDownloadStatus(explicitStatus);
      return [normalised.identity, normalised];
    }).filter(([identity]) => identity));
    const statuses = isPlainObject(response?.statuses) ? response.statuses : {};
    const responseStatus = stringValue(response?.status);
    for (const notice of noticeCatalog) {
      if (!requestedIdentities.includes(notice.identity)) continue;
      if (updates.has(notice.identity)) {
        notice.downloadStatus = updates.get(notice.identity).downloadStatus;
      } else if (Object.prototype.hasOwnProperty.call(statuses, notice.identity)) {
        notice.downloadStatus = normaliseDownloadStatus(statuses[notice.identity]);
      } else if (responseStatus) {
        notice.downloadStatus = normaliseDownloadStatus(responseStatus);
      }
    }
  }

  function startCatalogDownloadPolling() {
    window.clearTimeout(catalogDownloadPollTimer);
    catalogDownloadPollRemaining = 12;
    const poll = async () => {
      await loadNoticeCatalog({ silent: true });
      catalogDownloadPollRemaining -= 1;
      const active = noticeCatalog.some((notice) => ["queued", "downloading"].includes(notice.downloadStatus));
      if (active && catalogDownloadPollRemaining > 0) {
        catalogDownloadPollTimer = window.setTimeout(poll, 2500);
      }
    };
    catalogDownloadPollTimer = window.setTimeout(poll, 1200);
  }

  async function exportNoticeCatalog(button = elements.catalogExport) {
    const ids = catalogExportIdentities();
    if (catalogExportBusy || !activeQuery || !ids.length || queryConditionsChanged()) return;
    const queryId = activeQuery.id;
    catalogExportBusy = true;
    setButtonsBusy([button], true);
    elements.catalogExportStatus.hidden = false;
    elements.catalogExportStatus.textContent = `正在导出 ${ids.length} 条公告的信息和下载 URL…`;
    try {
      const response = await fetch("/api/notices/export", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]')?.content ?? "" },
        body: JSON.stringify({query_id: queryId, notice_ids: ids}),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.error || `表格导出失败（HTTP ${response.status}）`);
      }
      if (!String(response.headers.get("content-type") || "").includes("text/csv")) {
        throw new Error("导出接口未返回 CSV 表格，请刷新后重试。");
      }
      const blob = await response.blob();
      if (!blob.size) throw new Error("导出文件为空。");
      triggerBlobDownload(blob, `标讯信息及下载URL_${todayCompact()}.csv`);
      const rows = response.headers.get("X-Export-Rows");
      const links = response.headers.get("X-Export-URL-Rows");
      const notices = response.headers.get("X-Export-Notices") || ids.length;
      const summary = rows !== null && links !== null
        ? `已导出 ${notices} 条公告，共 ${rows} 行，其中 ${links} 行含附件下载 URL。`
        : `已导出 ${notices} 条公告的信息及下载 URL。`;
      elements.catalogExportStatus.textContent = summary + "浏览器已发起下载，输出目录的 exports 文件夹也保留一份。";
      showToast("表格已导出", summary, "success");
    } catch (error) {
      elements.catalogExportStatus.textContent = `未导出：${humanError(error)}`;
      showToast("表格未导出", humanError(error), "error", 7000);
    } finally {
      catalogExportBusy = false;
      setButtonsBusy([button], false);
      updateCatalogExportPresentation();
    }
  }

  function catalogServerExportUrl() {
    if (elements.catalogProvinceFilter?.value || elements.catalogDistrictFilter?.value) return "";
    const aiFilter = elements.catalogAiFilter.value || "relevant_pending";
    const downloadStatus = elements.catalogDownloadFilter.value;
    if (aiFilter === "relevant_pending" || ["queued", "downloading", "unavailable"].includes(downloadStatus)) {
      return "";
    }
    const params = new URLSearchParams();
    const mappings = [
      ["query", elements.catalogSearch.value.trim()],
      ["notice_type", elements.catalogTypeFilter.value],
      ["city", elements.catalogCityFilter.value],
      ["industry", elements.catalogIndustryFilter.value],
      ["download_status", downloadStatus],
    ];
    for (const [key, value] of mappings) {
      if (value) params.set(key, value);
    }
    params.set("relevance", aiFilter === "all" ? "all" : aiFilter);
    return `/api/notices/export?${params.toString()}`;
  }

  function exportFilteredCatalogCsv() {
    const headers = [
      "日(公告时间)", "数据标题", "公告类别", "客户名称", "一级行业标签", "城市",
      "客户联系人", "客户电话", "中标单位", "中标单位联系人-企业公示",
      "中标单位电话-企业公示", "项目标签", "公告页面缓存", "项目信息", "金额",
    ];
    const rows = filteredNoticeCatalog.map((notice) => [
      notice.publishedAt,
      notice.title,
      notice.noticeType,
      notice.purchaser,
      notice.industry,
      notice.city,
      notice.purchaserContact,
      notice.purchaserPhone,
      notice.vendor,
      notice.vendorContact,
      notice.vendorPhone,
      notice.tags.join("、"),
      notice.officialUrl,
      notice.projectInfo,
      primaryNoticeAmount(notice).value === "—" ? "" : primaryNoticeAmount(notice).value,
    ]);
    const csv = `\uFEFF${[headers, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n")}`;
    triggerBlobDownload(new Blob([csv], { type: "text/csv;charset=utf-8" }), `吉林省网络安全标讯_${todayCompact()}.csv`);
  }

  function csvCell(value) {
    let text = stringValue(value).replace(/\r?\n/g, " ");
    if (/^[=+\-@]/.test(text)) text = `'${text}`;
    return `"${text.replace(/"/g, '""')}"`;
  }

  function exportFilenameFromResponse(response) {
    const disposition = String(response.headers.get("content-disposition") || "");
    const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf8Match) {
      try { return decodeURIComponent(utf8Match[1]); } catch (_error) { /* fall through */ }
    }
    const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
    return plainMatch?.[1] || `吉林省网络安全标讯_${todayCompact()}.xlsx`;
  }

  function triggerBlobDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function triggerUrlDownload(value) {
    const safeUrl = safeCatalogUrl(value);
    if (!safeUrl) return;
    const link = document.createElement("a");
    link.href = safeUrl;
    link.rel = "noopener noreferrer";
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
  }

  function todayCompact() {
    const now = new Date();
    return `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}`;
  }

  function setCatalogState(kind, title, message) {
    elements.catalogState.hidden = false;
    elements.catalogState.className = `catalog-state is-${kind}`;
    const mark = elements.catalogState.querySelector(".catalog-state-mark");
    const heading = elements.catalogState.querySelector("strong");
    const body = elements.catalogState.querySelector("p");
    if (mark) mark.textContent = kind === "error" ? "!" : kind === "success" ? "✓" : "…";
    if (heading) heading.textContent = title;
    if (body) body.textContent = message;
  }

  function getCandidateConfig() {
    if (rawDirty) {
      const result = parseRawJson(true);
      if (!result.ok) return result;
      return { ok: true, value: result.value };
    }

    try {
      return { ok: true, value: collectConfigFromForm(configState) };
    } catch (error) {
      showToast("无法整理配置", humanError(error), "error");
      return { ok: false, error };
    }
  }

  function validateVisibleForm(candidate, options = {}) {
    clearFieldErrors();
    let firstInvalid = null;
    const markInvalid = (input, message) => {
      if (!input) return;
      input.classList.add("is-invalid");
      input.setCustomValidity(message);
      if (!firstInvalid) firstInvalid = input;
    };

    if (!elements.startDate.value) markInvalid(elements.startDate, "请选择开始日期");
    if (!elements.endDate.value) markInvalid(elements.endDate, "请选择结束日期");
    if (elements.startDate.value && elements.endDate.value && elements.startDate.value > elements.endDate.value) {
      markInvalid(elements.endDate, "结束日期不能早于开始日期");
    }
    if (!elements.outputDir.value.trim()) markInvalid(elements.outputDir, "请填写输出目录");
    if (!elements.databasePath.value.trim()) markInvalid(elements.databasePath, "请填写数据库路径");

    const seenSourceIds = new Set();
    for (const [index, source] of (Array.isArray(candidate.sources) ? candidate.sources : []).entries()) {
      if (source?.type !== "custom_web") continue;
      const item = elements.sourceList.querySelector(`[data-source-index="${index}"]`);
      if (!item) continue;
      const idInput = item.querySelector('[data-custom-field="id"]');
      const nameInput = item.querySelector('[data-custom-field="name"]');
      const urlsInput = item.querySelector('[data-custom-field="start_urls"]');
      const authMode = item.querySelector('[data-custom-field="auth_mode"]')?.value || "none";
      const loginUrlInput = item.querySelector('[data-custom-field="login_url"]');
      const usernameFieldInput = item.querySelector('[data-custom-field="username_field"]');
      const passwordFieldInput = item.querySelector('[data-custom-field="password_field"]');
      const extraInput = item.querySelector('[data-custom-field="extra_fields"]');
      const id = idInput?.value.trim() || "";
      if (!nameInput?.value.trim()) markInvalid(nameInput, "请填写网站名称");
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$/.test(id)) {
        markInvalid(idInput, "来源 ID 必须为 3–64 位，并以字母或数字开头");
      } else if (id && seenSourceIds.has(id)) {
        markInvalid(idInput, "来源 ID 不能重复");
      }
      if (id) seenSourceIds.add(id);

      const urls = String(urlsInput?.value || "").split(/\r?\n/).map((url) => url.trim()).filter(Boolean);
      if (urls.length === 0 || urls.some((url) => !isHttpUrl(url))) {
        markInvalid(urlsInput, "请至少填写一个有效的 http:// 或 https:// 入口网址");
      }
      const validUrls = urls.filter(isHttpUrl).map((url) => new URL(url));
      if (["browser", "basic", "form"].includes(authMode)) {
        const authOrigins = new Set(validUrls.map((url) => url.origin.toLowerCase()));
        if (validUrls.some((url) => url.protocol !== "https:")) {
          markInvalid(urlsInput, "需要登录的网站必须使用 HTTPS，防止账号密码明文传输");
        } else if (authOrigins.size > 1) {
          markInvalid(urlsInput, "一套登录凭据只能用于一个协议、主机和端口");
        }
      }
      if (authMode === "form") {
        if (!isHttpUrl(loginUrlInput?.value)) markInvalid(loginUrlInput, "请填写有效的表单登录页 URL");
        const startOrigins = new Set(validUrls.map((url) => url.origin.toLowerCase()));
        if (isHttpUrl(loginUrlInput?.value)
          && !startOrigins.has(new URL(loginUrlInput.value).origin.toLowerCase())) {
          markInvalid(loginUrlInput, "登录页必须与至少一个采集入口使用相同协议、主机和端口");
        }
        if (!/^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/.test(usernameFieldInput?.value.trim() || "")) {
          markInvalid(usernameFieldInput, "用户名字段名格式无效");
        }
        if (!/^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/.test(passwordFieldInput?.value.trim() || "")) {
          markInvalid(passwordFieldInput, "密码字段名格式无效");
        }
      } else if (authMode === "browser" && loginUrlInput?.value.trim()) {
        const startOrigins = new Set(validUrls.map((url) => url.origin.toLowerCase()));
        if (!isHttpUrl(loginUrlInput.value)) {
          markInvalid(loginUrlInput, "请填写有效的浏览器登录页 URL，或留空使用第一个入口");
        } else if (!startOrigins.has(new URL(loginUrlInput.value).origin.toLowerCase())) {
          markInvalid(loginUrlInput, "登录页必须与采集入口使用相同协议、主机和端口");
        }
      }

      const extraResult = parseJsonObject(extraInput?.value);
      if (!extraResult.ok) {
        markInvalid(extraInput, `必须是 JSON 对象：${extraResult.error}`);
      } else if (!hasOnlyStringEntries(extraResult.value)) {
        markInvalid(extraInput, "高级附加字段的键和值都必须是字符串");
      }
    }

    if (options.requireReady) {
      const enabledSources = Array.isArray(candidate.sources)
        ? candidate.sources.filter((source) => source?.enabled !== false).length
        : 0;
      if (enabledSources === 0) {
        showToast("没有启用数据源", "至少启用一个官方来源后才能启动。", "warning", 6000);
        document.getElementById("source-settings")?.scrollIntoView({ behavior: "smooth", block: "start" });
        return false;
      }

      if (candidate.ai?.enabled === true) {
        const endpoint = String(candidate.ai.endpoint ?? "");
        const model = String(candidate.ai.model ?? "");
        const keyEnv = String(candidate.ai.api_key_env ?? "");
        if (!/^https?:\/\//i.test(endpoint) || endpoint.includes("your-model-provider")) {
          markInvalid(elements.aiEndpoint, "启用 AI 后请填写有效接口地址");
        } else {
          try {
            const endpointUrl = new URL(endpoint.replace("{model}", "model"));
            const isLoopback = ["127.0.0.1", "localhost", "[::1]"].includes(
              endpointUrl.hostname.toLowerCase(),
            );
            if (endpointUrl.protocol !== "https:"
              && !(isLoopback && candidate.http?.allow_private_hosts === true)) {
              markInvalid(elements.aiEndpoint, "模型接口必须使用 HTTPS；本机 HTTP 模型需显式允许私网地址");
            }
          } catch (_error) {
            markInvalid(elements.aiEndpoint, "启用 AI 后请填写有效接口地址");
          }
        }
        if (!model || model.includes("replace-with")) {
          markInvalid(elements.aiModel, "启用 AI 后请填写模型名称");
        }
        if (!keyEnv) markInvalid(elements.aiKeyEnv, "请填写密钥环境变量名");
      }
    }

    if (!elements.configForm.checkValidity() || firstInvalid) {
      firstInvalid = firstInvalid || elements.configForm.querySelector(":invalid");
      firstInvalid?.reportValidity();
      firstInvalid?.focus({ preventScroll: true });
      firstInvalid?.scrollIntoView({ behavior: "smooth", block: "center" });
      showToast("请检查表单", firstInvalid?.validationMessage || "部分字段格式不正确。", "warning", 5500);
      return false;
    }

    return true;
  }

  function clearFieldErrors() {
    for (const input of elements.configForm.querySelectorAll("input, select, textarea")) {
      input.classList.remove("is-invalid");
      input.setCustomValidity("");
    }
  }

  function isHttpUrl(value) {
    try {
      const url = new URL(String(value || ""));
      return (url.protocol === "http:" || url.protocol === "https:")
        && Boolean(url.hostname)
        && !url.username
        && !url.password;
    } catch (_error) {
      return false;
    }
  }

  function hasOnlyStringEntries(value) {
    return isPlainObject(value)
      && Object.entries(value).every(([key, entry]) => typeof key === "string" && typeof entry === "string");
  }

  async function refreshStatus(options = {}) {
    try {
      let response = await apiRequest(lastLogId > 0 ? `/api/status?after=${lastLogId}` : "/api/status");
      const status = response && typeof response === "object" && response.status
        ? response.status
        : response;
      if (!isPlainObject(status)) throw new Error("状态响应格式不正确。");

      const serverLastLogId = Number(status.last_log_id ?? 0);
      if (lastLogId > 0 && Number.isFinite(serverLastLogId) && serverLastLogId < lastLogId) {
        lastLogId = 0;
        statusLogBuffer = [];
        bufferTaskKey = "";
        response = await apiRequest("/api/status");
        Object.assign(status, response);
      }

      mergeStatusLogs(status);
      lastStatus = status;
      statusReadFailed = false;
      renderStatus(status);
      return status;
    } catch (error) {
      statusReadFailed = true;
      renderQueryProgress();
      setStatusClasses("error");
      elements.headerStatusText.textContent = "本地服务未连接";
      if (!options.silent) showToast("状态读取失败", humanError(error), "error");
      throw error;
    }
  }

  function renderStatus(status) {
    const downloadsActive = Object.keys(status.active_downloads || {}).length > 0;
    const downloadsPending = downloadsActive || pendingDownloadIdentities.size > 0;
    const serverState = String(status.state ?? "").toLowerCase();
    const running = isOperationActive(status);
    const exitCode = normaliseExitCode(status.exit_code);
    const operation = status.operation ? String(status.operation) : "";
    const operationLabel = OPERATION_LABELS[operation] || operation || "—";
    const taskKey = `${operation}|${status.started_at ?? ""}`;

    if (taskKey !== currentTaskKey && status.started_at) {
      currentTaskKey = taskKey;
      hiddenLogCount = 0;
      renderedLogKeys = [];
      visibleLogLines = [];
      elements.logOutput.replaceChildren();
    }

    let state = "idle";
    let title = "等待启动";
    let headerText = "本地服务已连接";
    const configuredPendingLogins = new Set(
      (Array.isArray(browserLoginCheckpoint?.pending) ? browserLoginCheckpoint.pending : [])
        .map((item) => String(item?.source_id || ""))
        .filter(Boolean),
    );
    const loginPreflight = !running && Array.from(browserLoginStates.entries()).some(([sourceId, session]) => (
      configuredPendingLogins.has(sourceId)
      && ["starting", "waiting", "verifying", "challenge_required"].includes(
        String(session?.state || "").toLowerCase(),
      )
    ));

    if (serverState === "starting") {
      state = "running";
      title = `${operationLabel}正在启动`;
      headerText = title;
    } else if (serverState === "stopping") {
      state = "warning";
      title = `${operationLabel}正在停止`;
      headerText = title;
    } else if (running) {
      state = "running";
      title = `${operationLabel}进行中`;
      headerText = title;
    } else if (serverState === "stopped") {
      state = "warning";
      title = `${operationLabel}已停止`;
      headerText = "上次任务已停止";
    } else if (serverState === "completed") {
      state = "success";
      title = `${operationLabel}已完成`;
      headerText = "上次任务已完成";
    } else if (serverState === "partial") {
      state = "warning";
      title = status.operation === "sample" ? "试采已结束（限定范围）" : `${operationLabel}部分完成`;
      headerText = status.operation === "sample" ? "试采结果可供筛选" : "上次任务部分完成";
    } else if (serverState === "failed") {
      state = "error";
      title = `${operationLabel}未完成`;
      headerText = "上次任务出现错误";
    } else if (exitCode === 0 && status.finished_at) {
      state = "success";
      title = `${operationLabel}已完成`;
      headerText = "上次任务已完成";
    } else if (exitCode === 2 && status.finished_at) {
      state = "warning";
      title = `${operationLabel}部分完成`;
      headerText = "上次任务部分完成";
    } else if (exitCode !== null && status.finished_at) {
      state = "error";
      title = `${operationLabel}未完成`;
      headerText = "上次任务出现错误";
    }

    if (loginPreflight) {
      state = "warning";
      title = "等待网站登录或验证码";
      headerText = "正在等待网站登录";
    }

    setStatusClasses(state);
    elements.headerStatusText.textContent = headerText;
    elements.statusTitle.textContent = title;
    elements.statusOperation.textContent = loginPreflight ? "采集启动检查" : operationLabel;
    elements.statusStarted.textContent = formatDateTime(status.started_at);
    elements.statusFinished.textContent = formatDateTime(status.finished_at);
    elements.statusExitCode.textContent = exitCode === null ? "—" : String(exitCode);

    // Do not undo the local busy lock while runCollection is waiting for a
    // browser login.  Otherwise periodic /api/status polling makes the run
    // button clickable again and starts duplicate waiting loops.
    if (!elements.runButton.classList.contains("is-busy")) {
      elements.runButton.disabled = running || downloadsPending;
    }
    if (!elements.stopButton.classList.contains("is-busy")) {
      elements.stopButton.disabled = !running || serverState === "stopping";
    }
    if (!elements.verifyButton.classList.contains("is-busy")) {
      elements.verifyButton.disabled = running || downloadsPending;
    }
    if (!queryLaunching) elements.queryPlatforms.disabled = running || downloadsPending || (elements.catalogProvinceFilter && !regionsReady);
    if (!elements.quickSample.classList.contains("is-busy")) elements.quickSample.disabled = running || downloadsPending;
    elements.beginnerProgress.textContent = running
      ? `${operationLabel}进行中，清单每6秒自动更新。当前已有 ${noticeCatalog.length} 条，可边看边筛选。`
      : downloadsPending ? "原文件正在下载，清单每6秒更新。可以继续查看、导出已有结果；下载完成后可启动新查询。"
      : "填写客户要求后点击“查询各平台”；每次都会重新向平台检索，并列出本次结果。";
    if (((["run", "sample", "query"].includes(operation) && running) || downloadsActive
        || pendingDownloadIdentities.size || serverDownloadsActive !== downloadsActive)
        && Date.now() - catalogLastLiveRefresh >= 6000) {
      catalogLastLiveRefresh = Date.now();
      loadNoticeCatalog({silent:true}).catch(()=>null);
      serverDownloadsActive = downloadsActive;
    }

    if (["run", "sample", "query"].includes(operation) && status.finished_at && !running && taskKey !== catalogRefreshTaskKey) {
      catalogRefreshTaskKey = taskKey;
      loadNoticeCatalog({ silent: true }).catch(() => null);
    }

    renderLogs(Array.isArray(status.logs) ? status.logs : []);
    renderQueryProgress();
  }

  function renderLogs(logs) {
    if (hiddenLogCount > logs.length) hiddenLogCount = 0;
    const visible = logs.slice(hiddenLogCount).map(normaliseLog);
    const keys = visible.map((line) => line.key);
    const samePrefix = renderedLogKeys.every((key, index) => keys[index] === key);

    if (!samePrefix || renderedLogKeys.length > keys.length) {
      elements.logOutput.replaceChildren();
      renderedLogKeys = [];
      visibleLogLines = [];
    }

    if (visible.length > 0) {
      elements.logOutput.querySelector(".console-placeholder")?.remove();
    }

    for (let index = renderedLogKeys.length; index < visible.length; index += 1) {
      const line = visible[index];
      const lineElement = document.createElement("div");
      lineElement.className = `log-line${line.level ? ` is-${line.level}` : ""}`;
      const text = document.createElement("span");
      text.textContent = line.text;
      lineElement.append(text);
      elements.logOutput.append(lineElement);
    }

    renderedLogKeys = keys;
    visibleLogLines = visible;
    elements.logCount.textContent = `${visible.length} 行`;

    if (visible.length === 0) {
      const placeholder = elements.consolePlaceholder.cloneNode(true);
      placeholder.id = "console-placeholder";
      elements.logOutput.replaceChildren(placeholder);
      renderedLogKeys = [];
    } else if (elements.autoScroll.checked) {
      elements.logOutput.scrollTop = elements.logOutput.scrollHeight;
    }
  }

  function mergeStatusLogs(status) {
    const taskKey = `${status.operation ?? ""}|${status.started_at ?? ""}`;
    if (taskKey && bufferTaskKey && taskKey !== bufferTaskKey) {
      statusLogBuffer = [];
      hiddenLogCount = 0;
    }
    if (taskKey) bufferTaskKey = taskKey;

    const incoming = Array.isArray(status.logs) ? status.logs : [];
    const knownIds = new Set(
      statusLogBuffer
        .map((entry) => isPlainObject(entry) ? entry.id : null)
        .filter((id) => id !== null && id !== undefined)
        .map(String),
    );

    for (const entry of incoming) {
      const id = isPlainObject(entry) ? entry.id : null;
      if (id !== null && id !== undefined) {
        if (knownIds.has(String(id))) continue;
        knownIds.add(String(id));
      }
      statusLogBuffer.push(entry);
    }

    if (statusLogBuffer.length > 2000) {
      const removedCount = statusLogBuffer.length - 2000;
      statusLogBuffer = statusLogBuffer.slice(-2000);
      hiddenLogCount = Math.max(0, hiddenLogCount - removedCount);
    }

    const reportedLastId = Number(status.last_log_id ?? 0);
    const incomingLastId = incoming.reduce((maximum, entry) => {
      const id = Number(isPlainObject(entry) ? entry.id : 0);
      return Number.isFinite(id) ? Math.max(maximum, id) : maximum;
    }, 0);
    lastLogId = Math.max(lastLogId, Number.isFinite(reportedLastId) ? reportedLastId : 0, incomingLastId);
    status.logs = statusLogBuffer.slice();
  }

  function normaliseLog(entry, index) {
    if (typeof entry === "string") {
      return { text: entry, level: inferLogLevel(entry), key: `text:${index}:${entry}` };
    }
    if (isPlainObject(entry)) {
      const message = String(entry.message ?? entry.text ?? entry.line ?? JSON.stringify(entry));
      const timestamp = entry.timestamp ? formatLogTimestamp(entry.timestamp) : "";
      const text = timestamp ? `${timestamp}  ${message}` : message;
      const suppliedLevel = String(entry.level ?? "").toLowerCase();
      const level = ["error", "warning", "success"].includes(suppliedLevel)
        ? suppliedLevel
        : inferLogLevel(message);
      const identity = entry.id ?? `${entry.timestamp ?? ""}:${index}:${message}`;
      return { text, level, key: `entry:${identity}` };
    }
    const text = String(entry ?? "");
    return { text, level: inferLogLevel(text), key: `value:${index}:${text}` };
  }

  function inferLogLevel(text) {
    if (/\b(error|fatal|failed|exception)\b|错误|失败|异常/i.test(text)) return "error";
    if (/\b(warn|warning|partial)\b|警告|受限|部分完成/i.test(text)) return "warning";
    if (/\b(done|success|completed)\b|成功|已完成|校验通过/i.test(text)) return "success";
    return "";
  }

  function clearVisibleLogs() {
    const allLogs = Array.isArray(lastStatus?.logs) ? lastStatus.logs : [];
    hiddenLogCount = allLogs.length;
    renderedLogKeys = [];
    visibleLogLines = [];
    renderLogs(allLogs);
    showToast("日志已清屏", "只清除当前页面显示，不会删除任务记录。", "info");
  }

  async function copyLogs() {
    if (visibleLogLines.length === 0) {
      showToast("没有可复制的日志", "启动任务后再试。", "warning");
      return;
    }
    const text = visibleLogLines.map((line) => line.text).join("\n");
    try {
      await copyText(text);
      showToast("日志已复制", `${visibleLogLines.length} 行内容已复制到剪贴板。`, "success");
    } catch (error) {
      showToast("复制失败", humanError(error), "error");
    }
  }

  function scheduleStatusPoll(delay) {
    window.clearTimeout(statusPollTimer);
    const nextDelay = document.hidden ? Math.max(delay, 7000) : delay;
    statusPollTimer = window.setTimeout(async () => {
      try {
        const [status] = await Promise.all([
          refreshStatus({ silent: true }),
          refreshBrowserLoginStatus({ silent: true }).catch(() => browserLoginStates),
        ]);
        scheduleStatusPoll(isOperationActive(status) ? 1400 : 3500);
      } catch (_error) {
        scheduleStatusPoll(6000);
      }
    }, nextDelay);
  }

  function updateAiPresentation() {
    const enabled = elements.aiEnabled.checked;
    elements.aiToggleLabel.textContent = enabled ? "已启用" : "已关闭";
    elements.aiSettingsBody.classList.toggle("is-disabled", !enabled);
    elements.aiSettingsBody.setAttribute("aria-disabled", String(!enabled));
    elements.apiKey.placeholder = enabled ? "填写 Key，或留空使用已保存的 Key" : "AI 关闭时无需填写";
    updateSummaryFromVisibleFields();
  }

  function updateSourcePresentation() {
    const toggles = Array.from(elements.sourceList.querySelectorAll("[data-source-enabled]"));
    const enabledCount = toggles.filter((input) => input.checked).length;
    elements.sourceCount.textContent = `${enabledCount} 个启用`;
    elements.summarySources.textContent = `${enabledCount} / ${toggles.length}`;
  }

  function updateSummary() {
    const data = rawDirty ? null : configState;
    if (!data) {
      updateSummaryFromVisibleFields();
      return;
    }
    elements.summaryDate.textContent = formatDateRange(data.start_date, data.end_date);
    const sources = Array.isArray(data.sources) ? data.sources : [];
    const enabledCount = sources.filter((source) => source?.enabled !== false).length;
    elements.summarySources.textContent = `${enabledCount} / ${sources.length}`;
    elements.summaryAi.textContent = data.ai?.enabled ? (data.ai.model || "已启用") : "未启用";
  }

  function updateSummaryFromVisibleFields() {
    elements.summaryDate.textContent = formatDateRange(elements.startDate.value, elements.endDate.value);
    updateSourcePresentation();
    elements.summaryAi.textContent = elements.aiEnabled.checked
      ? (elements.aiModel.value.trim() || "已启用")
      : "未启用";
  }

  function updateDirtyPresentation() {
    const dirty = formDirty || rawDirty;
    for (const button of elements.saveButtons) {
      button.dataset.dirty = String(dirty);
      const label = button.querySelector(".button-label");
      if (label) label.textContent = dirty ? "保存配置 · 有修改" : "保存配置";
    }
  }

  function setJsonState(state, label, message) {
    elements.jsonState.className = `json-state is-${state}`;
    elements.jsonState.replaceChildren();
    const dot = document.createElement("span");
    dot.setAttribute("aria-hidden", "true");
    dot.textContent = "●";
    elements.jsonState.append(dot, document.createTextNode(` ${label}`));
    elements.jsonMessage.textContent = message;
    elements.jsonMessage.classList.toggle("is-error", state === "invalid");
  }

  function setStatusClasses(state) {
    for (const dot of [elements.headerStatusDot, elements.statusDot]) {
      dot.classList.remove("is-idle", "is-running", "is-success", "is-warning", "is-error");
      dot.classList.add(`is-${state}`);
    }
  }

  function setButtonsBusy(buttons, busy) {
    for (const button of buttons.filter(Boolean)) {
      button.classList.toggle("is-busy", busy);
      button.setAttribute("aria-busy", String(busy));
      if (busy) {
        button.dataset.wasDisabled = String(button.disabled);
        button.disabled = true;
      } else {
        const running = isOperationActive(lastStatus);
        if (button.dataset.action === "run" || button.dataset.action === "verify") {
          button.disabled = running;
        } else if (button.dataset.action === "stop") {
          button.disabled = !running || lastStatus?.state === "stopping";
        } else {
          button.disabled = button.dataset.wasDisabled === "true";
        }
        delete button.dataset.wasDisabled;
      }
    }
  }

  function toggleApiKeyVisibility() {
    const reveal = elements.apiKey.type === "password";
    elements.apiKey.type = reveal ? "text" : "password";
    elements.toggleApiKey.textContent = reveal ? "隐藏" : "显示";
    elements.toggleApiKey.setAttribute("aria-label", reveal ? "隐藏 API 密钥" : "显示 API 密钥");
    elements.toggleApiKey.setAttribute("aria-pressed", String(reveal));
  }

  function setupSectionNavigation() {
    const links = Array.from(document.querySelectorAll(".nav-item"));
    const targets = links
      .map((link) => document.querySelector(link.getAttribute("href")))
      .filter(Boolean);
    let scheduled = false;
    const update = () => {
      scheduled = false;
      const offset = (document.querySelector(".topbar")?.offsetHeight || 72) + 48;
      const visible = targets.filter(target => !target.hidden && target.getClientRects().length);
      let current = visible[0];
      for (const target of visible) {
        if (target.getBoundingClientRect().top <= offset) current = target;
      }
      if (!current) return;
      for (const link of links) {
        const active = link.getAttribute("href") === `#${current.id}`;
        link.classList.toggle("is-active", active);
        if (active) link.setAttribute("aria-current", "location");
        else link.removeAttribute("aria-current");
      }
    };
    const schedule = () => {
      if (scheduled) return;
      scheduled = true;
      window.requestAnimationFrame(update);
    };
    window.addEventListener("scroll", schedule, {passive: true});
    window.addEventListener("resize", schedule);
    schedule();
  }

  async function apiRequest(path, options = {}) {
    const method = String(options.method ?? "GET").toUpperCase();
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");

    if (method !== "GET") {
      const csrf = document.querySelector('meta[name="csrf-token"]')?.content ?? "";
      headers.set("X-CSRF-Token", csrf);
    }

    const request = {
      method,
      headers,
      credentials: "same-origin",
      cache: method === "GET" ? "no-store" : "default",
    };

    if (Object.prototype.hasOwnProperty.call(options, "body")) {
      headers.set("Content-Type", "application/json; charset=utf-8");
      request.body = JSON.stringify(options.body);
    }

    let response;
    try {
      response = await fetch(path, request);
    } catch (error) {
      throw new Error(`无法连接本地服务：${error.message || "网络请求失败"}`);
    }

    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_error) {
        data = text;
      }
    }

    if (!response.ok) {
      const message = errorMessageFromPayload(data) || `请求失败（HTTP ${response.status}）`;
      const error = new Error(message);
      error.status = response.status;
      error.payload = data;
      throw error;
    }

    return data ?? {};
  }

  function showToast(title, message, type = "success", duration = 3800) {
    const toast = document.createElement("div");
    toast.className = `toast${type !== "success" ? ` is-${type}` : ""}`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");

    const mark = document.createElement("span");
    mark.className = "toast-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = type === "error" ? "!" : type === "warning" ? "!" : type === "info" ? "i" : "✓";

    const copy = document.createElement("div");
    const heading = document.createElement("strong");
    heading.textContent = title;
    const body = document.createElement("p");
    body.textContent = message;
    copy.append(heading, body);

    const close = document.createElement("button");
    close.type = "button";
    close.setAttribute("aria-label", "关闭通知");
    close.textContent = "×";
    close.addEventListener("click", () => removeToast(toast));

    toast.append(mark, copy, close);
    elements.toastRegion.append(toast);
    window.setTimeout(() => removeToast(toast), duration);
  }

  function removeToast(toast) {
    if (!toast?.isConnected || toast.classList.contains("is-leaving")) return;
    toast.classList.add("is-leaving");
    window.setTimeout(() => toast.remove(), 190);
  }

  function showPageNotice(title, message, type = "info") {
    elements.pageNoticeTitle.textContent = title;
    elements.pageNoticeMessage.textContent = message;
    elements.pageNotice.className = `notice notice-${type}`;
    elements.pageNotice.hidden = false;
  }

  function assignNumber(target, key, input) {
    const value = input.value.trim();
    if (value === "") {
      delete target[key];
      return;
    }
    target[key] = Number(value);
  }

  function setValue(input, value) {
    input.value = value === null || value === undefined ? "" : String(value);
  }

  function numericInputValue(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "";
    return String(value);
  }

  function deepClone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function prettyJson(value) {
    return JSON.stringify(value, null, 2);
  }

  function isPlainObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function cssEscape(value) {
    if (window.CSS?.escape) return window.CSS.escape(String(value));
    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function toCamelCase(value) {
    return value.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
  }

  function readableJsonError(error) {
    const original = error?.message || "JSON 格式错误";
    const position = original.match(/position\s+(\d+)/i);
    if (!position) return original;
    const index = Number(position[1]);
    const before = elements.rawJson.value.slice(0, index);
    const line = before.split("\n").length;
    const column = index - before.lastIndexOf("\n");
    return `第 ${line} 行、第 ${column} 列附近格式有误。`;
  }

  function formatDateRange(start, end) {
    if (!start && !end) return "—";
    if (!start || !end) return start || end;
    return `${shortDate(start)} → ${shortDate(end)}`;
  }

  function shortDate(value) {
    const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return match ? `${match[1]}.${match[2]}.${match[3]}` : String(value);
  }

  function formatDateTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  function formatLogTimestamp(value) {
    if (!value) return "";
    const date = new Date(value);
    if (!Number.isNaN(date.getTime())) {
      return new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(date);
    }
    const match = String(value).match(/(?:T|\s)(\d{2}:\d{2}:\d{2})/);
    return match ? match[1] : String(value);
  }

  function isOperationActive(status) {
    if (!status || typeof status !== "object") return false;
    const state = String(status.state ?? "").toLowerCase();
    return status.running === true || ["starting", "running", "stopping"].includes(state);
  }

  function normaliseExitCode(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function errorMessageFromPayload(payload) {
    if (typeof payload === "string") return payload.trim();
    if (!isPlainObject(payload)) return "";
    const candidate = payload.detail ?? payload.error ?? payload.message;
    if (typeof candidate === "string") return candidate;
    if (Array.isArray(candidate)) {
      return candidate.map((item) => typeof item === "string" ? item : item?.msg || JSON.stringify(item)).join("；");
    }
    return candidate ? JSON.stringify(candidate) : "";
  }

  function responseMessage(response, fallback) {
    if (typeof response === "string" && response.trim()) return response.trim();
    if (isPlainObject(response)) {
      const message = response.message ?? response.detail;
      if (typeof message === "string" && message.trim()) return message.trim();
    }
    return fallback;
  }

  function humanError(error) {
    return error?.message || String(error || "未知错误");
  }

  async function copyText(text) {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.append(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) throw new Error("浏览器未允许访问剪贴板。");
  }
})();
