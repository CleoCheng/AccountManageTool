// State Management
const state = {
  accounts: [],
  stats: { total: 0, google: 0, chatgpt: 0, types: {}, devices: {}, other: 0, active: 0, deactivated: 0, available: 0, unavailable: 0, health: {}, antigravity: {} },
  dicts: [],
  attributes: [],
  customFilters: {}, // attr_key -> value
  editingAttrId: null,
  filterType: "All",
  statusFilter: "All",
  healthFilter: "All",
  antigravityFilter: "All",
  deviceFilter: "All",
  searchQuery: "",
  theme: "dark",
  viewMode: "compact", // 'compact' | 'full' | 'table'
  expandedCards: {}, // id -> boolean
  revealedFields: {}, // `${id}_${field}` -> boolean
  lastTotpSlot: Math.floor(Date.now() / 30000),
  parsedBatchRecords: [],
  importAccountType: "",
  // Proxy Node Selector & Lock State
  proxyEnv: null,
  nodeModalAccountId: null,
  selectedRegion: "全部",
  selectedProxyUrl: "http://127.0.0.1:17899",
  selectedNodeName: "",
  _initialFilterRestored: false,
  nodeCodeStats: {},
  autoMailCodePolling: false,
  // Maintenance, Warming, OAuth & Appeal State
  loginToolsAccountId: null,
  topRecoveryEmails: [],
  appealChannels: [],
  activeQuickCredAccountId: null,
  activeRtAccountId: null,
  oauthSessionId: "",
  oauthAuthUrl: "",
  oauthPollTimer: null,
  // System Settings State
  currentDictTab: "account_type",
  editingDictId: null,
  sortMode: "default",
};

window.__kvOnAccountUpdated = () => {
  loadDashboard();
};

async function callApi(method, ...args) {
  if (window.pywebview && window.pywebview.api && window.pywebview.api[method]) {
    return await window.pywebview.api[method](...args);
  }
  throw new Error("pywebview API not ready");
}

function syncDomainEmailLoginHint(accountType) {
  const hint = document.getElementById("domain-email-login-hint");
  if (!hint) return;

  const isDomainEmail = accountType === "域名邮箱";
  hint.style.display = isDomainEmail ? "block" : "none";
  if (!isDomainEmail) return;

  const url = state.settings?.domain_email_login_url || "https://mail.ggb6.com";
  const link = hint.querySelector("a");
  if (link) {
    link.href = url;
    link.textContent = url.replace(/^https?:\/\//i, "");
  }
}

let appInitialized = false;
let totpTickerTimer = null;

function initApp() {
  initAllCustomSelects();
  if (appInitialized) return;
  appInitialized = true;
  loadDashboard();
  startZeroOverheadTotpTicker();
}

window.addEventListener("pywebviewready", () => {
  initApp();
});

document.addEventListener("DOMContentLoaded", () => {
  initAllCustomSelects();
  setTimeout(() => {
    if (!appInitialized && window.pywebview && window.pywebview.api) {
      initApp();
    }
  }, 120);
});

let dashboardRequestId = 0;

async function loadDashboard() {
  const requestId = ++dashboardRequestId;
  try {
    const data = await callApi(
      "get_dashboard",
      state.filterType,
      state.searchQuery,
      state.statusFilter,
      state.healthFilter,
      state.antigravityFilter,
      state.deviceFilter,
      state.customFilters
    );
    if (requestId !== dashboardRequestId) return;
    if (!state._initialFilterRestored) {
      state._initialFilterRestored = true;
      const savedFilter = data.settings?.last_filter_type;
      if (savedFilter && savedFilter !== state.filterType) {
        state.filterType = savedFilter;
        return await loadDashboard();
      }
    }
    if (data.dicts) state.dicts = data.dicts;
    if (data.attributes) state.attributes = data.attributes;
    if (renderFilterDropdowns()) return await loadDashboard();
    state.accounts = data.accounts || [];
    if (Array.isArray(state.modalAccountsPool) && state.modalAccountsPool.length && state.accounts.length) {
      const freshById = new Map(state.accounts.map((a) => [a.id, a]));
      state.modalAccountsPool = state.modalAccountsPool.map((prev) => freshById.get(prev.id) || prev);
    }
    if (data.node_code_stats) {
      state.nodeCodeStats = data.node_code_stats;
    }
    if (Array.isArray(data.top_recovery_emails)) {
      state.topRecoveryEmails = data.top_recovery_emails;
      syncRecoveryEmailDatalist();
    }
    if (Array.isArray(data.appeal_channels)) {
      state.appealChannels = data.appeal_channels;
    }
    updateWorkBuddyQueue(data.workbuddy);
    state.stats = data.stats || { total: 0, google: 0, chatgpt: 0, types: {}, devices: {}, other: 0, active: 0, deactivated: 0, available: 0, unavailable: 0, health: {}, antigravity: {} };
    if (data.settings) {
      state.settings = data.settings;
      applyTheme(data.settings.theme || "dark", false);
      const savedMode = data.settings.view_mode === "grid" ? "compact" : data.settings.view_mode || "compact";
      applyViewMode(savedMode, false);
    }
    renderStats(data.health_counts);
    syncTopBarPills();
    syncGoogleTopActions();
    syncAntigravitySelectStyle();
    renderAccounts();
    renderLoginTools();
    if (state.filterType === "域名邮箱" && state.autoMailCodePolling) {
      startMailCodePolling();
    } else {
      stopMailCodePolling();
    }
    if (typeof syncAutoMailPollButton === "function") {
      syncAutoMailPollButton();
    }
  } catch (err) {
    console.error("Load dashboard failed:", err);
  }
}

function renderHealthStats(counts) {
  document.getElementById("stat-health-available").textContent = counts?.["可用"] ?? "—";
  document.getElementById("stat-health-unavailable").textContent = counts?.["不可用"] ?? "—";
  document.getElementById("stat-health-unreviewed").textContent = counts?.["未核对"] ?? "—";
  document.getElementById("stat-pill-unreviewed").hidden =
    state.filterType !== "Google" &&
    !(state.filterType === "All" && (counts?.["未核对"] > 0 || state.healthFilter === "未核对"));
}

function renderStats(healthCounts) {
  document.getElementById("stat-total").textContent = state.stats.total ?? 0;
  renderHealthStats(healthCounts);

  // Dynamically populate and update topbar platform dropdown
  const topSel = document.getElementById("topbar-platform-select");
  if (topSel) {
    const typeDicts = (state.dicts || []).filter((d) => d.dict_type === "account_type");
    const typeCounts = state.stats.types || {};
    let optionsHtml = `<option value="All">全部平台 (${state.stats.total || 0})</option>`;
    if (typeDicts.length > 0) {
      optionsHtml += typeDicts
        .map((d) => {
          const cnt = typeCounts[d.dict_key] !== undefined ? typeCounts[d.dict_key] : 0;
          return `<option value="${escapeHtml(d.dict_key)}">${escapeHtml(d.dict_label || d.dict_key)} (${cnt})</option>`;
        })
        .join("");
    } else {
      const claudeCount = state.stats.claude ?? typeCounts.Claude ?? 0;
      optionsHtml += `
        <option value="ChatGPT">ChatGPT (${state.stats.chatgpt || 0})</option>
        <option value="Google">Google (${state.stats.google || 0})</option>
        <option value="Claude">Claude (${claudeCount})</option>
      `;
    }
    topSel.innerHTML = optionsHtml;
    topSel.value = state.filterType || "All";
    syncCustomSelect(topSel);
  }

  syncPlatformDotColor();
  syncGoogleTopActions();
}

function syncGoogleTopActions() {
  const isGoogle = state.filterType === "Google";
  const btnRt = document.getElementById("btn-rt-oauth");
  const btnAppeal = document.getElementById("btn-appeal");
  if (btnRt) btnRt.hidden = !isGoogle;
  if (btnAppeal) btnAppeal.hidden = !isGoogle;
}

function syncPlatformDotColor() {
  const dot = document.getElementById("topbar-platform-dot");
  if (!dot) return;
  const p = state.filterType;
  if (p === "Claude") {
    dot.style.backgroundColor = "var(--color-claude)";
    dot.style.boxShadow = "0 0 6px rgba(217, 119, 87, 0.6)";
  } else if (p === "ChatGPT") {
    dot.style.backgroundColor = "var(--color-chatgpt)";
    dot.style.boxShadow = "0 0 6px rgba(16, 185, 129, 0.6)";
  } else if (p === "Google") {
    dot.style.backgroundColor = "var(--color-google)";
    dot.style.boxShadow = "0 0 6px rgba(245, 158, 11, 0.6)";
  } else if (p === "WorkBuddy") {
    dot.style.backgroundColor = "#818cf8";
    dot.style.boxShadow = "0 0 6px rgba(129, 140, 248, 0.6)";
  } else if (p === "域名邮箱") {
    dot.style.backgroundColor = "#38bdf8";
    dot.style.boxShadow = "0 0 6px rgba(56, 189, 248, 0.6)";
  } else if (p === "Qcoder") {
    dot.style.backgroundColor = "#f472b6";
    dot.style.boxShadow = "0 0 6px rgba(244, 114, 182, 0.6)";
  } else {
    dot.style.backgroundColor = "var(--accent-primary)";
    dot.style.boxShadow = "0 0 6px rgba(59, 130, 246, 0.6)";
  }
}

function syncTopBarPills() {
  const isTotal =
    (state.filterType === "All" || !state.filterType) &&
    (state.healthFilter === "All" || !state.healthFilter) &&
    (state.antigravityFilter === "All" || !state.antigravityFilter) &&
    (state.deviceFilter === "All" || !state.deviceFilter);
  const pillTotal = document.getElementById("stat-pill-total");
  const pillPlatWrap = document.getElementById("stat-pill-platform-wrap");
  const pillAvail = document.getElementById("stat-pill-available");
  const pillUnavail = document.getElementById("stat-pill-unavailable");
  const pillUnreviewed = document.getElementById("stat-pill-unreviewed");

  if (pillTotal) pillTotal.classList.toggle("active", isTotal);
  if (pillPlatWrap) pillPlatWrap.classList.toggle("active", state.filterType !== "All");
  if (pillAvail) pillAvail.classList.toggle("active", state.healthFilter === "可用");
  if (pillUnavail) pillUnavail.classList.toggle("active", state.healthFilter === "不可用");
  if (pillUnreviewed) {
    const selected = state.healthFilter === "未核对";
    pillUnreviewed.classList.toggle("active", selected);
    pillUnreviewed.setAttribute("aria-pressed", String(selected));
  }

  syncPlatformDotColor();
  syncGoogleTopActions();
}

function isAttributeApplicable(attr, platform) {
  if (!attr) return false;
  const bound = attr.bound_platforms || "All";
  if (platform === "All") {
    return bound === "All";
  }
  if (bound === "All") return true;
  let list = Array.isArray(attr.platforms_list) ? attr.platforms_list : [];
  if (list.length === 0) {
    try {
      list = typeof bound === "string" ? JSON.parse(bound) : bound;
    } catch (e) {
      list = [bound];
    }
  }
  return list.includes(platform);
}

function renderFilterDropdowns() {
  // 1. Platform Filter (in toolbar)
  const platSel = document.getElementById("platform-filter-select");
  if (platSel) {
    const typeDicts = (state.dicts || []).filter((d) => d.dict_type === "account_type");
    let html = `<option value="All">全部平台</option>`;
    if (typeDicts.length > 0) {
      html += typeDicts
        .map((d) => `<option value="${escapeHtml(d.dict_key)}">${d.icon ? escapeHtml(d.icon) + " " : ""}${escapeHtml(d.dict_label || d.dict_key)}</option>`)
        .join("");
    } else {
      html += `
        <option value="ChatGPT">🤖 ChatGPT</option>
        <option value="Google">🔍 Google</option>
        <option value="Claude">✴️ Claude</option>
      `;
    }
    platSel.innerHTML = html;
    platSel.value = state.filterType || "All";
    syncCustomSelect(platSel);
  }

  // 2. Dynamic Attribute Filters Container
  const container = document.getElementById("dynamic-filter-attributes-container");
  if (!container) return;

  const activePlatform = state.filterType || "All";
  const allAttrs = state.attributes || [];

  // Find attributes with is_filter != 0 and attr_type == 'select' applicable to activePlatform
  const applicableAttrs = allAttrs.filter(
    (a) => a.is_filter && a.attr_type === "select" && isAttributeApplicable(a, activePlatform)
  );

  const applicableKeys = new Set(applicableAttrs.map((a) => a.attr_key));
  let filtersChanged = false;
  for (const [key, field] of [["device_tag", "deviceFilter"], ["antigravity_tier", "antigravityFilter"]]) {
    if (!applicableKeys.has(key) && state[field] !== "All") {
      state[field] = "All";
      filtersChanged = true;
    }
  }
  for (const [key, value] of Object.entries(state.customFilters)) {
    if (!applicableKeys.has(key) && value !== "All") {
      delete state.customFilters[key];
      filtersChanged = true;
    }
  }

  container.innerHTML = "";

  applicableAttrs.forEach((attr) => {
    const wrap = document.createElement("div");
    wrap.className = "health-select-wrap";

    const sel = document.createElement("select");
    sel.id = `filter-${attr.attr_key}`;
    sel.className = "health-filter-select";
    if (attr.attr_key === "antigravity_tier") {
      sel.classList.add("ag-filter-select");
    }
    sel.title = `按${attr.attr_label}筛选`;

    const defOpt = document.createElement("option");
    defOpt.value = "All";
    defOpt.textContent = `全部${attr.attr_label}`;
    sel.appendChild(defOpt);

    const dictItems = (state.dicts || []).filter((d) => d.dict_type === attr.dict_type);
    dictItems.forEach((d) => {
      const opt = document.createElement("option");
      opt.value = d.dict_key;
      opt.textContent = `${d.icon ? `${d.icon} ` : ""}${d.dict_label || d.dict_key}`;
      sel.appendChild(opt);
    });

    if (attr.attr_key === "health_status") {
      if ((activePlatform === "Google" || activePlatform === "All") && !dictItems.some((d) => d.dict_key === "未核对")) {
        sel.add(new Option("未核对", "未核对"));
      }
      sel.value = state.healthFilter || "All";
      sel.onchange = () => onHealthSelectChange(sel.value);
    } else if (attr.attr_key === "antigravity_tier") {
      sel.value = state.antigravityFilter || "All";
      sel.onchange = () => onAntigravitySelectChange(sel.value);
    } else if (attr.attr_key === "device_tag") {
      sel.value = state.deviceFilter || "All";
      sel.onchange = () => setDeviceFilter(sel.value);
    } else {
      sel.value = state.customFilters?.[attr.attr_key] || "All";
      sel.onchange = () => setCustomFilter(attr.attr_key, sel.value);
    }

    wrap.appendChild(sel);
    container.appendChild(wrap);

    enhanceSelectToCustom(sel);

    if (attr.attr_key === "antigravity_tier") {
      syncAntigravitySelectStyle();
    }
  });
  return filtersChanged;
}

function setCustomFilter(attrKey, val) {
  if (!state.customFilters) state.customFilters = {};
  state.customFilters[attrKey] = val || "All";
  const sel = document.getElementById(`filter-${attrKey}`);
  if (sel && sel.value !== state.customFilters[attrKey]) {
    sel.value = state.customFilters[attrKey];
  }
  syncCustomSelect(sel);
  loadDashboard();
}

function setDeviceFilter(dev) {
  state.deviceFilter = dev || "All";
  const sel = document.getElementById("filter-device_tag") || document.getElementById("device-filter-select");
  if (sel && sel.value !== state.deviceFilter) {
    sel.value = state.deviceFilter;
  }
  syncCustomSelect(sel);
  syncTopBarPills();
  loadDashboard();
}

function resetTopBarFilters() {
  state.healthFilter = "All";
  state.antigravityFilter = "All";
  state.deviceFilter = "All";
  setFilterType("All");
}

function setFilterType(type) {
  const nextType = type || "All";
  const platformChanged = state.filterType !== nextType;
  state._initialFilterRestored = true;
  state.filterType = nextType;
  if (state.healthFilter === "未核对" && nextType !== "Google" && nextType !== "All") {
    state.healthFilter = "All";
  }
  if (platformChanged) renderHealthStats();
  if (state.filterType === "域名邮箱" && state.autoMailCodePolling) {
    startMailCodePolling();
  } else {
    stopMailCodePolling();
  }
  if (typeof syncAutoMailPollButton === "function") {
    syncAutoMailPollButton();
  }
  const topSel = document.getElementById("topbar-platform-select");
  if (topSel && topSel.value !== state.filterType) {
    topSel.value = state.filterType;
  }
  const platSel = document.getElementById("platform-filter-select");
  if (platSel && platSel.value !== state.filterType) {
    platSel.value = state.filterType;
  }
  syncCustomSelect(topSel);
  syncCustomSelect(platSel);
  renderFilterDropdowns();
  syncTopBarPills();
  syncGoogleTopActions();
  syncPlatformDotColor();
  callApi("save_settings", { last_filter_type: state.filterType }).catch(() => {});
  loadDashboard();
}

function onHealthSelectChange(val) {
  setHealthFilter(val);
}

function setHealthFilter(health) {
  state.healthFilter = health || "All";
  const sel = document.getElementById("filter-health_status") || document.getElementById("health-filter-select");
  if (sel && sel.value !== state.healthFilter) {
    sel.value = state.healthFilter;
  }
  syncCustomSelect(sel);
  syncTopBarPills();
  loadDashboard();
}

function syncAntigravitySelectStyle() {
  const sel = document.getElementById("filter-antigravity_tier") || document.getElementById("ag-filter-select");
  if (!sel) return;
  const targets = [sel];
  if (sel._customContainer) targets.push(sel._customContainer);

  targets.forEach((t) => {
    t.classList.remove("ag-select-blue", "ag-select-gold", "ag-select-none");
    if (state.antigravityFilter === "蓝色Pro" || state.antigravityFilter === "蓝色背景色Pro") {
      t.classList.add("ag-select-blue");
    } else if (state.antigravityFilter === "金色Pro" || state.antigravityFilter === "金色背景色Pro") {
      t.classList.add("ag-select-gold");
    } else if (state.antigravityFilter === "无") {
      t.classList.add("ag-select-none");
    }
  });
}

function onAntigravitySelectChange(val) {
  setAntigravityFilter(val);
}

function setAntigravityFilter(tier) {
  state.antigravityFilter = tier || "All";
  const sel = document.getElementById("filter-antigravity_tier") || document.getElementById("ag-filter-select");
  if (sel && sel.value !== state.antigravityFilter) {
    sel.value = state.antigravityFilter;
  }
  syncCustomSelect(sel);
  syncAntigravitySelectStyle();
  syncTopBarPills();
  loadDashboard();
}

function getHealthMeta(status) {
  switch (status) {
    case "封号":
      return { key: "banned", label: "封号", dotClass: "dot-banned" };
    case "人机":
      return { key: "captcha", label: "人机", dotClass: "dot-captcha" };
    case "短信":
      return { key: "sms", label: "短信", dotClass: "dot-sms" };
    case "验证":
      return { key: "verify", label: "验证", dotClass: "dot-verify" };
    case "不可用":
      return { key: "unavailable", label: "不可用", dotClass: "dot-unavailable" };
    case "可用":
    default:
      return { key: "available", label: "可用", dotClass: "dot-available" };
  }
}

function getAccountHealthMeta(account) {
  const meta = getHealthMeta(account.health_status);
  if (account.account_type !== "Google") return meta;
  if (meta.key === "available") {
    if (account.deactivated_date || account.availability === "不可用") return getHealthMeta("不可用");
    return account.custom_fields?.google_reviewed_at
      ? { ...meta, label: "已确认登录" }
      : { ...getHealthMeta("不可用"), key: "unreviewed", label: "未核对" };
  }
  const pendingLabels = { captcha: "待验证 · 人机", sms: "待验证 · 短信", verify: "待验证 · 身份" };
  return { ...meta, label: pendingLabels[meta.key] || meta.label };
}

function getWarmingInfo(account) {
  return account.warming || { phase: "warming", label: "待复核", hint: "" };
}

function getAntigravityMeta(tier) {
  const t = (tier || "无").trim();
  if (t === "蓝色Pro" || t === "蓝色背景色Pro" || t === "蓝Pro") {
    return {
      key: "blue",
      value: "蓝色Pro",
      label: "Pro",
      badgeText: "Pro",
      dotClass: "dot-ag-blue",
    };
  }
  if (t === "金色Pro" || t === "金色背景色Pro" || t === "金Pro") {
    return {
      key: "gold",
      value: "金色Pro",
      label: "Pro",
      badgeText: "Pro",
      dotClass: "dot-ag-gold",
    };
  }
  return {
    key: "none",
    value: "无",
    label: "无",
    badgeText: "无",
    dotClass: "dot-ag-none",
  };
}

function getDeviceMeta(tag) {
  const t = (tag || "主机").trim();
  if (t === "iPhone") {
    return { key: "iphone", label: "iPhone", icon: "📱", badgeText: "📱 iPhone" };
  }
  if (t === "Pad") {
    return { key: "pad", label: "Pad", icon: "📱", badgeText: "📱 Pad" };
  }
  return { key: "host", label: t, icon: "🖥️", badgeText: `🖥️ ${t}` };
}

function toggleHealthMenu(event, accountId) {
  event.stopPropagation();
  const menuEl = document.getElementById(`health-menu-${accountId}`);
  if (!menuEl) return;
  const isShown = menuEl.style.display === "block";
  closeAllHealthPopovers();
  if (!isShown) {
    menuEl.style.display = "block";
  }
}

function toggleAntigravityMenu(event, accountId) {
  event.stopPropagation();
  const menuEl = document.getElementById(`ag-menu-${accountId}`);
  if (!menuEl) return;
  const isShown = menuEl.style.display === "block";
  closeAllHealthPopovers();
  if (!isShown) {
    menuEl.style.display = "block";
  }
}

function toggleDeviceMenu(event, accountId) {
  event.stopPropagation();
  const menuEl = document.getElementById(`dev-menu-${accountId}`);
  if (!menuEl) return;
  const isShown = menuEl.style.display === "block";
  closeAllHealthPopovers();
  if (!isShown) {
    menuEl.style.display = "block";
  }
}

function closeAllHealthPopovers() {
  document.querySelectorAll(".u-health-popover, .u-ag-popover, .u-device-popover").forEach((el) => {
    el.style.display = "none";
  });
  closeAllCustomSelects();
}

async function onQuickSelectDevice(accountId, nextDevice) {
  closeAllHealthPopovers();
  try {
    const res = await callApi("set_device_tag", accountId, nextDevice);
    if (res && res.ok) {
      showToast(`✓ 已标记设备为「${nextDevice}」`);
      await loadDashboard();
    } else {
      showToast(res.error || "设备标记更新失败");
    }
  } catch (err) {
    showToast("设备标记更新异常: " + err.message);
  }
}

document.addEventListener("click", () => {
  closeAllHealthPopovers();
});

async function onQuickSelectHealth(accountId, nextStatus) {
  closeAllHealthPopovers();
  try {
    const res = await callApi("set_health_status", accountId, nextStatus);
    if (res && res.ok) {
      const label = res.account ? getAccountHealthMeta(res.account).label : nextStatus;
      showToast(`✓ 已记录「${label}」`);
      if (res.account && state.activeQuickCredAccountId === accountId) {
        renderGoogleReview(res.account);
        renderQuickCredWarming(res.account);
      }
      await loadDashboard();
    } else {
      showToast(res.error || "状态更新失败");
    }
  } catch (err) {
    showToast("状态更新异常: " + err.message);
  }
}

async function onQuickSelectAntigravity(accountId, nextTier) {
  closeAllHealthPopovers();
  try {
    const res = await callApi("set_antigravity_tier", accountId, nextTier);
    if (res && res.ok) {
      showToast(`✓ 已标记 Antigravity 为「${nextTier}」`);
      await loadDashboard();
    } else {
      showToast(res.error || "Antigravity 标记更新失败");
    }
  } catch (err) {
    showToast("Antigravity 标记更新异常: " + err.message);
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatTotpCode(code) {
  if (!code || code.length !== 6) return "--- ---";
  return `${code.slice(0, 3)} ${code.slice(3)}`;
}

function isRevealed(id, field) {
  return !!state.revealedFields[`${id}_${field}`];
}

function toggleReveal(id, field) {
  const key = `${id}_${field}`;
  state.revealedFields[key] = !state.revealedFields[key];
  renderAccounts();
}

function toggleCardExpand(id) {
  state.expandedCards[id] = !state.expandedCards[id];
  renderAccounts();
}

function getUnifiedMetaSummary(item) {
  const parts = [];
  if (item.purchase_date) {
    if (item.deactivated_date) {
      parts.push(`${item.purchase_date} ~ ${item.deactivated_date}`);
    } else {
      parts.push(`购入 ${item.purchase_date}`);
    }
  }
  if (item.reg_year || item.reg_region) {
    const yr = [item.reg_year, item.reg_region].filter(Boolean).join(" · ");
    parts.push(yr);
  }
  return parts.length ? parts.join(" | ") : "标准账号";
}

function getFpSummary(item) {
  const fp = item.fp_config || {};
  const code = fp.profile_code || `FP-${String(item.id).padStart(3, "0")}`;
  const proxyNode = fp.proxy_node || "";
  const locked = !!fp.node_locked;
  const proxyPort = fp.proxy ? fp.proxy.replace("http://127.0.0.1:", ":") : "";

  const isClaude = item.account_type === "Claude";
  const tz = fp.timezone || (isClaude ? "America/Los_Angeles" : "系统默认");
  const locale = fp.locale || (isClaude ? "en-US, en" : "系统默认");
  const tooltip = `浏览器档案 [${code}]\n绑定节点: ${proxyNode || "未绑定"} (${locked ? "已锁定" : "未锁定"})\n端口: ${fp.proxy || "默认"}\n时区/语言: ${tz} · ${locale}\n档案目录: data/browsers/acc_${item.id}`;

  return {
    code,
    proxyNode,
    locked,
    proxyPort,
    shortDesc: proxyNode || (fp.proxy ? "使用固定代理" : "未绑定代理"),
    tooltip,
  };
}

function getSortedAccounts(list) {
  const arr = Array.isArray(list) ? list.slice() : [];
  if (state.sortMode === "warming_stale") {
    arr.sort((a, b) => {
      const ta = a.browser_last_opened_at || "";
      const tb = b.browser_last_opened_at || "";
      if (ta !== tb) return ta.localeCompare(tb);
      return (a.browser_open_count || 0) - (b.browser_open_count || 0);
    });
  } else if (state.sortMode === "warming_recent") {
    arr.sort((a, b) => (b.browser_last_opened_at || "").localeCompare(a.browser_last_opened_at || ""));
  } else if (state.sortMode === "warming_count") {
    arr.sort((a, b) => (b.browser_open_count || 0) - (a.browser_open_count || 0));
  }
  return arr;
}

function setSortMode(mode) {
  state.sortMode = mode || "default";
  renderAccounts();
}

function renderAccounts() {
  const container = document.getElementById("accounts-container");
  const emptyState = document.getElementById("empty-state");

  const accountsToDisplay = getSortedAccounts(state.accounts);

  renderWorkBuddyToolbar(accountsToDisplay);

  if (!accountsToDisplay || accountsToDisplay.length === 0) {
    container.innerHTML = "";
    emptyState.style.display = "block";
    return;
  }

  emptyState.style.display = "none";
  const remaining = 30 - (Math.floor(Date.now() / 1000) % 30);

  if (state.viewMode === "table") {
    container.className = "accounts-table-wrapper";
    container.innerHTML = renderTableView(remaining, accountsToDisplay);
    return;
  }

  container.className = "accounts-grid";
  container.innerHTML = accountsToDisplay
    .map((item) => renderCardItem(item, remaining))
    .join("");
}


const mailboxRegistrationStates = ["未完成", "进行中", "已完成"];

async function toggleMailboxMarker(button, accountId, field) {
  const account = state.accounts.find((item) => item.id === accountId && item.account_type === "域名邮箱");
  if (!account || button.disabled) return;
  const fields = account.custom_fields || {};
  const current = mailboxRegistrationStates.indexOf(fields.wb_reg_status || "未完成");
  const value = field === "chatgpt_bound"
    ? fields.chatgpt_bound !== true
    : mailboxRegistrationStates[(current + 1) % mailboxRegistrationStates.length];
  button.disabled = true;
  try {
    const result = await callApi("set_domain_email_metadata", accountId, field, value);
    if (!result?.ok) {
      showToast(result?.error || "保存标记失败");
      return;
    }
    await loadDashboard();
  } catch (error) {
    showToast("保存标记失败: " + error.message);
  } finally {
    button.disabled = false;
  }
}

function renderWorkBuddyItemBadges(item) {
  if (!item) return "";
  const cf = item.custom_fields || {};
  const parts = [];
  if (item.account_type === "域名邮箱") {
    const bound = cf.chatgpt_bound === true;
    const statuses = {
      "未完成": ["wb-pending", "⏳ 未完成注册"],
      "进行中": ["wb-progress", `🔄 进行中${cf.wb_reg_step_label ? ` · ${escapeHtml(cf.wb_reg_step_label)}` : ""}`],
      "已完成": ["wb-done", "✅ 已完成注册"],
    };
    const registration = mailboxRegistrationStates.includes(cf.wb_reg_status) ? cf.wb_reg_status : "未完成";
    const [statusClass, statusLabel] = statuses[registration];
    parts.push(
      `<button type="button" class="u-wb-badge ${statusClass}" title="点击切换 WorkBuddy 注册状态" onclick="toggleMailboxMarker(this, ${item.id}, 'wb_reg_status')">${statusLabel}</button>`,
      `<button type="button" class="u-wb-badge ${bound ? "wb-done" : "wb-unbound"}" aria-pressed="${bound}" title="点击切换 ChatGPT 绑定标记" onclick="toggleMailboxMarker(this, ${item.id}, 'chatgpt_bound')">ChatGPT ${bound ? "已绑定" : "未绑定"}</button>`
    );
  }
  if (cf.wb_credential && cf.wb_credential.accessToken) {
    parts.push('<span class="u-wb-badge wb-cred" title="已保存 WorkBuddy2API 登录凭据，可勾选导出 JSON">🔑 凭据已存</span>');
  }
  return parts.join("");
}

function renderCardItem(item, remaining) {
  const showAcc = isRevealed(item.id, "account");
  const displayAcc = showAcc ? item.account : item.masked_account;

  const hasTotp = item.secret_key && item.totp && item.totp.valid;
  const totpFormatted = hasTotp ? formatTotpCode(item.totp.code) : "无 2FA";

  const usage = item.usage || { status: "active", status_label: "使用中", duration_text: "使用中" };
  const isDeactivated = usage.status === "deactivated";
  const statusText =
    usage.duration_text && usage.duration_text !== usage.status_label
      ? usage.duration_text
      : usage.status_label;

  const hMeta = getAccountHealthMeta(item);
  const agMeta = getAntigravityMeta(item.antigravity_tier || "无");

  const isPwdModified = !!item.password_modified;
  const pwdFlagHtml = `<button class="u-2fa-flag ${isPwdModified ? "modified" : "unmodified"}" onclick="toggleCardPasswordModified(${item.id}, ${isPwdModified ? 0 : 1})" title="点击切换密码是否已修改标记">
      ${isPwdModified ? "🔑 密码已改 ✓" : "🔑 初始密码"}
    </button>`;

  const is2faModified = !!item.twofa_modified;
  const twofaFlagHtml = item.secret_key
    ? `<button class="u-2fa-flag ${is2faModified ? "modified" : "unmodified"}" onclick="toggleCardTwofaModified(${item.id}, ${is2faModified ? 0 : 1})" title="点击切换 2FA 是否已修改标记">
        ${is2faModified ? "🛡️ 2FA已改 ✓" : "🛡️ 初始2FA"}
      </button>`
    : `<span class="u-2fa-flag none" title="未配置 2FA 动态秘钥">🛡️ 无2FA</span>`;

  const fpInfo = getFpSummary(item);
  const isClaude = item.account_type === "Claude";
  const isUnboundClaude = isClaude && !fpInfo.proxyNode && !(item.fp_config && item.fp_config.proxy);
  let nodePillText = fpInfo.proxyNode
    ? `${fpInfo.locked ? "🔒" : "🔓"} ${fpInfo.proxyNode}${fpInfo.proxyPort ? ` (${fpInfo.proxyPort})` : ""}`
    : (isClaude ? "⚠️ 未绑定代理" : "🔓 绑定节点");
  if (item.account_type === "域名邮箱" && fpInfo.proxyNode) {
    const c = (state.nodeCodeStats && state.nodeCodeStats[fpInfo.proxyNode]) || 0;
    nodePillText = `${fpInfo.locked ? "🔒" : "🔓"} ${fpInfo.proxyNode} (${c}/2 已收码)`;
  }

  let totpBoxHtml;
  if (item.account_type === "域名邮箱") {
    if (item.custom_fields && item.custom_fields.latest_code) {
      totpBoxHtml = `
        <div class="u-totp-box mail-code-box has-code" onclick="copyTotpCode(this, ${item.id})" title="最新邮件验证码: ${escapeHtml(item.custom_fields.latest_code)} · 点击直接复制">
          <span class="u-totp-label mail-code-label">邮件码</span>
          <span class="u-totp-code mail-code-val" id="totp-code-${item.id}">${escapeHtml(item.custom_fields.latest_code)}</span>
          <span class="u-totp-sec">(${formatTimeAgo(item.custom_fields.code_received_at)})</span>
          <button type="button" class="mail-code-refresh" aria-label="刷新此邮箱验证码" onclick="event.stopPropagation(); refreshSingleEmailCode(${item.id}, this)">刷新</button>
        </div>
      `;
    } else {
      totpBoxHtml = `
        <div class="u-totp-box mail-code-box pending" onclick="copyTotpCode(this, ${item.id})" title="暂无验证码 · 点击立即连接邮箱查信">
          <span class="u-totp-label mail-code-label">邮件码</span>
          <span class="u-totp-code pending-mail-code" id="totp-code-${item.id}">暂无 (查信)</span>
          <span class="u-totp-sec">🔄</span>
          <button type="button" class="mail-code-refresh" aria-label="刷新此邮箱验证码" onclick="event.stopPropagation(); refreshSingleEmailCode(${item.id}, this)">刷新</button>
        </div>
      `;
    }
  } else {
    totpBoxHtml = `
      <div class="u-totp-box" onclick="copyTotpCode(null, ${item.id})" title="点击复制 6 位 2FA 动态验证码">
        <span class="u-totp-label">2FA码</span>
        <span class="u-totp-code" id="totp-code-${item.id}">${totpFormatted}</span>
        ${hasTotp ? `<span class="u-totp-sec">(<span class="card-rem-sec">${remaining}</span>s)</span>` : ""}
      </div>
    `;
  }

  const metaLeft = getUnifiedMetaSummary(item);
  const showDrawer = state.viewMode === "full" || !!state.expandedCards[item.id];

  const devMeta = getDeviceMeta(item.device_tag || "主机");
  const devAttr = (state.attributes || []).find((a) => a.attr_key === "device_tag");
  const showDevBadge = !devAttr || isAttributeApplicable(devAttr, item.account_type);

  const agAttr = (state.attributes || []).find((a) => a.attr_key === "antigravity_tier");
  const showAgBadge = !agAttr || isAttributeApplicable(agAttr, item.account_type);

  // 1-Click Quick Device Tag Dropdown (主机 / iPhone / Pad)
  const devBadgeHtml = `
    <div class="u-device-dropdown" data-acc-id="${item.id}">
      <button class="u-device-btn dev-${devMeta.key}" onclick="toggleDeviceMenu(event, ${item.id})" title="点击切换设备标记">
        <span>${devMeta.icon}</span>
        <span>${escapeHtml(devMeta.label)}</span>
        <span class="health-arrow">▾</span>
      </button>
      <div class="u-device-popover" id="dev-menu-${item.id}" style="display: none;">
        <div class="popover-title">标记所在设备</div>
        <button class="device-opt ${devMeta.label === '主机' ? 'active' : ''}" onclick="onQuickSelectDevice(${item.id}, '主机')">
          <span>🖥️</span><span>主机 (主电脑)</span>
        </button>
        <button class="device-opt ${devMeta.label === 'iPhone' ? 'active' : ''}" onclick="onQuickSelectDevice(${item.id}, 'iPhone')">
          <span>📱</span><span>iPhone (手机)</span>
        </button>
        <button class="device-opt ${devMeta.label === 'Pad' ? 'active' : ''}" onclick="onQuickSelectDevice(${item.id}, 'Pad')">
          <span>📱</span><span>Pad (平板)</span>
        </button>
      </div>
    </div>
  `;

  // 1-Click Quick Antigravity Option Marker Dropdown (纯净 Pro 标签)
  const agBadgeHtml = `
    <div class="u-ag-dropdown" data-acc-id="${item.id}">
      <button class="u-ag-btn ag-${agMeta.key}" onclick="toggleAntigravityMenu(event, ${item.id})" title="点击切换 Antigravity 选项标记">
        <span class="badge-dot ${agMeta.dotClass}"></span>
        <span class="ag-btn-label">${escapeHtml(agMeta.badgeText)}</span>
        <span class="health-arrow">▾</span>
      </button>
      <div class="u-ag-popover" id="ag-menu-${item.id}" style="display: none;">
        <div class="popover-title">Antigravity 标记</div>
        <button class="ag-opt opt-none ${agMeta.key === 'none' ? 'active' : ''}" onclick="onQuickSelectAntigravity(${item.id}, '无')">
          <span class="badge-dot dot-ag-none"></span><span>无 (未标记)</span>
        </button>
        <button class="ag-opt opt-blue ${agMeta.key === 'blue' ? 'active' : ''}" onclick="onQuickSelectAntigravity(${item.id}, '蓝色Pro')">
          <span class="badge-dot dot-ag-blue"></span><span>💎 Pro</span>
        </button>
        <button class="ag-opt opt-gold ${agMeta.key === 'gold' ? 'active' : ''}" onclick="onQuickSelectAntigravity(${item.id}, '金色Pro')">
          <span class="badge-dot dot-ag-gold"></span><span>👑 Pro</span>
        </button>
      </div>
    </div>
  `;

  // 1-Click Quick Health Status Dropdown
  const healthBadgeHtml = `
    <div class="u-health-dropdown" data-acc-id="${item.id}">
      <button class="u-health-btn health-${hMeta.key}" onclick="toggleHealthMenu(event, ${item.id})" title="点击快捷维护账号可用状态">
        <span class="badge-dot ${hMeta.dotClass}"></span>
        <span class="health-btn-label">${escapeHtml(hMeta.label)}</span>
        <span class="health-arrow">▾</span>
      </button>
      <div class="u-health-popover" id="health-menu-${item.id}" style="display: none;">
        <div class="popover-title">维护账号可用状态</div>
        <button class="health-opt ${hMeta.key === 'available' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '可用')">
          <span class="badge-dot dot-available"></span><span>${item.account_type === "Google" ? "已确认登录" : "可用 (正常使用)"}</span>
        </button>
        <button class="health-opt ${hMeta.key === 'banned' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '封号')">
          <span class="badge-dot dot-banned"></span><span>封号 (官方封禁)</span>
        </button>
        <button class="health-opt ${hMeta.key === 'captcha' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '人机')">
          <span class="badge-dot dot-captcha"></span><span>人机 (盾 / 验证码)</span>
        </button>
        <button class="health-opt ${hMeta.key === 'sms' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '短信')">
          <span class="badge-dot dot-sms"></span><span>短信 (手机验证)</span>
        </button>
        <button class="health-opt ${hMeta.key === 'verify' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '验证')">
          <span class="badge-dot dot-verify"></span><span>验证 (邮箱/密码)</span>
        </button>
        <button class="health-opt ${hMeta.key === 'unavailable' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '不可用')">
          <span class="badge-dot dot-unavailable"></span><span>不可用 (闲置停用)</span>
        </button>
      </div>
    </div>
  `;

  // Detail Drawer Content
  let drawerHtml = "";
  if (showDrawer) {
    const showPwd = isRevealed(item.id, "password");
    const showSec = isRevealed(item.id, "secret");
    const showInitSec = isRevealed(item.id, "init_secret");
    const showRec = isRevealed(item.id, "recovery");

    drawerHtml = `
      <div class="u-detail-drawer">
        <div class="detail-row">
          <span class="detail-key">可用状态</span>
          <span class="detail-val" style="display:inline-flex;align-items:center;gap:6px;">
            <span class="badge-dot ${hMeta.dotClass}"></span>
            <strong>${escapeHtml(hMeta.label)}</strong>
          </span>
          <div class="detail-ops">
            <button class="u-peek-btn" onclick="openEditModal(${item.id})">在表单维护</button>
          </div>
        </div>
        ${
          showAgBadge
            ? `
        <div class="detail-row">
          <span class="detail-key">Antigravity</span>
          <span class="detail-val" style="display:inline-flex;align-items:center;gap:6px;">
            <span class="badge-dot ${agMeta.dotClass}"></span>
            <strong>${escapeHtml(agMeta.label)}</strong>
          </span>
          <div class="detail-ops">
            <button class="u-peek-btn" onclick="openEditModal(${item.id})">修改标记</button>
          </div>
        </div>`
            : ""
        }
        ${renderCustomDrawerRows(item)}
        <div class="detail-row">
          <span class="detail-key">浏览器档案</span>
          <span class="detail-val">[${escapeHtml(fpInfo.code)}] ${escapeHtml(fpInfo.shortDesc)}</span>
          <div class="detail-ops">
            <button class="u-peek-btn" onclick="openNodeModal(${item.id})">${fpInfo.locked ? "🔒 节点已锁" : "配置节点"}</button>
          </div>
        </div>
        <div class="detail-row">
          <span class="detail-key">完整账号</span>
          <span class="detail-val">${escapeHtml(item.account)}</span>
          <div class="detail-ops">
            <button class="copy-btn" onclick="copyFieldValue(this, ${item.id}, 'account', '账号邮箱')">复制</button>
          </div>
        </div>
        <div class="detail-row">
          <span class="detail-key">登录密码</span>
          <span class="detail-val">${escapeHtml(showPwd ? item.password : item.masked_password)}</span>
          <div class="detail-ops">
            <button class="u-peek-btn" onclick="toggleReveal(${item.id}, 'password')">${showPwd ? "隐藏" : "明文"}</button>
            <button class="copy-btn" onclick="copyFieldValue(this, ${item.id}, 'password', '登录密码')">复制</button>
          </div>
        </div>
        ${
          item.secret_key
            ? `
        <div class="detail-row">
          <span class="detail-key">${is2faModified ? "新2FA秘钥" : "2FA秘钥"}</span>
          <span class="detail-val">${escapeHtml(showSec ? item.secret_key : item.masked_secret)}</span>
          <div class="detail-ops">
            <button class="u-peek-btn" onclick="toggleReveal(${item.id}, 'secret')">${showSec ? "隐藏" : "明文"}</button>
            <button class="copy-btn" onclick="copyFieldValue(this, ${item.id}, 'secret_key', '2FA秘钥')">复制</button>
          </div>
        </div>`
            : ""
        }
        ${
          item.initial_secret_key
            ? `
        <div class="detail-row">
          <span class="detail-key">初始2FA</span>
          <span class="detail-val">${escapeHtml(showInitSec ? item.initial_secret_key : item.masked_initial_secret)}</span>
          <div class="detail-ops">
            <button class="u-peek-btn" onclick="toggleReveal(${item.id}, 'init_secret')">${showInitSec ? "隐藏" : "明文"}</button>
            <button class="copy-btn" onclick="copyFieldValue(this, ${item.id}, 'initial_secret_key', '初始2FA秘钥')">复制</button>
          </div>
        </div>`
            : ""
        }
        ${
          item.recovery_email
            ? `
        <div class="detail-row">
          <span class="detail-key">辅助邮箱</span>
          <span class="detail-val">${escapeHtml(showRec ? item.recovery_email : item.masked_recovery_email)}</span>
          <div class="detail-ops">
            <button class="u-peek-btn" onclick="toggleReveal(${item.id}, 'recovery')">${showRec ? "隐藏" : "明文"}</button>
            <button class="copy-btn" onclick="copyFieldValue(this, ${item.id}, 'recovery_email', '辅助邮箱')">复制</button>
          </div>
        </div>`
            : ""
        }
      </div>
    `;
  }

  // Device Chip (Prominent, dedicated flex chip that never gets crushed)
  const deviceChipHtml = item.note
    ? `<span class="u-device-chip" title="所在设备/备注: ${escapeHtml(item.note)}">
        <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect>
          <line x1="8" y1="21" x2="16" y2="21"></line>
          <line x1="12" y1="17" x2="12" y2="21"></line>
        </svg>
        <span class="device-text">${escapeHtml(item.note)}</span>
      </span>`
    : "";

  const secInfo = item.security || { status: "unmodified", label: "⚠️ 未改密", completed: 0, total: 4 };
  const warmInfo = getWarmingInfo(item);
  const secBadgeHtml = `<button class="sec-badge sec-${escapeHtml(secInfo.status)}" onclick="openQuickCredModal(${item.id})" title="点击进入4步安全改密与历史凭据归档 (${secInfo.completed || 0}/4)">${escapeHtml(secInfo.label)}</button>`;
  const warmBadgeHtml = `<button class="warm-badge warm-${escapeHtml(warmInfo.phase)}" onclick="openQuickCredModal(${item.id})" title="${escapeHtml(warmInfo.hint || warmInfo.label)}">🌱 ${escapeHtml(warmInfo.label)}</button>`;
  const rtBadgeHtml =
    item.account_type === "Google" && (item.has_rt || (item.gcp_status && item.gcp_status !== "none"))
      ? `<button class="rt-pill rt-${escapeHtml(item.rt_status || "none")}" onclick="openRtOAuthModal(${item.id})" title="RT 状态: ${escapeHtml(item.rt_status || "none")} · GCP: ${escapeHtml(item.gcp_status || "none")} · 订阅: ${escapeHtml(item.subscription_plan || "Free")}">🛸 ${escapeHtml(item.rt_status === "active" ? (item.gcp_status === "validation" ? "需验证" : (item.subscription_plan || "RT在线")) : (item.rt_status || "RT"))}</button>`
      : "";

  return `
    <article class="u-card health-border-${hMeta.key} ag-card-${showAgBadge ? agMeta.key : "none"} ${isDeactivated ? "is-deactivated" : ""}" data-id="${item.id}">
      <!-- Line 1: Header (Account Identity & 1-Click Health Status Badge) -->
      <div class="u-row-top">
        <div class="u-identity">
          ${renderWorkBuddyCheckbox(item)}
          <span class="u-platform ${escapeHtml(item.account_type)}">${escapeHtml(item.account_type)}</span>
          <span class="u-fp-badge" title="${escapeHtml(fpInfo.tooltip)}">${escapeHtml(fpInfo.code)}</span>
          <span class="u-email" onclick="copyFieldValue(null, ${item.id}, 'account', '账号邮箱')" title="点击直接复制账号: ${escapeHtml(displayAcc)}">
            ${escapeHtml(displayAcc)}
          </span>
          <button class="u-peek-btn" onclick="toggleReveal(${item.id}, 'account')" title="切换显示完整邮箱">
            ${showAcc ? "隐" : "明"}
          </button>
        </div>

        <div class="u-top-right">
          ${healthBadgeHtml}
        </div>
      </div>

      <!-- Line 2: Security Flags & Antigravity Option Marker Row -->
      <div class="u-sec-row">
        <div class="u-sec-left">
          ${secBadgeHtml}
          ${pwdFlagHtml}
          ${twofaFlagHtml}
        </div>
        <div class="u-sec-right" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
          ${renderWorkBuddyItemBadges(item)}
          ${rtBadgeHtml}
          ${showDevBadge ? devBadgeHtml : ""}
          ${showAgBadge ? agBadgeHtml : ""}
        </div>
      </div>

      <!-- Line 3: Usage Timeline & Proxy Node Row (时长与绑定节点独立行) -->
      <div class="u-status-row">
        <div class="u-status-left" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
          <span class="u-usage-badge ${usage.status}">
            <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="10"></circle>
              <polyline points="12 6 12 12 16 14"></polyline>
            </svg>
            ${escapeHtml(statusText)}
          </span>
          ${warmBadgeHtml}
          ${
            (item.browser_open_count || 0) > 0
              ? `<span class="u-usage-badge" title="最近开窗: ${escapeHtml(item.browser_last_opened_at || "未知")}${fpInfo.proxyNode ? ` · 节点: ${escapeHtml(fpInfo.proxyNode)}` : ""}">🖥️ ${item.browser_open_count}次</span>`
              : ""
          }
        </div>
        <div class="u-status-right">
          <button class="u-node-pill ${fpInfo.locked ? "locked" : ""} ${isUnboundClaude ? "unbound-warn" : ""}" onclick="openNodeModal(${item.id})" title="专属代理节点">
            ${escapeHtml(nodePillText)}
          </button>
        </div>
      </div>

      <!-- Line 3: Unified TOTP + 1-Click Copy + Fingerprint Browser Launch Strip -->
      <div class="u-action-bar">
        ${totpBoxHtml}

        <div class="u-copy-group">
          <button class="copy-btn" onclick="copyFieldValue(this, ${item.id}, 'account', '账号')">账号</button>
          <button class="copy-btn" onclick="copyFieldValue(this, ${item.id}, 'password', '密码')">密码</button>
          ${
            item.secret_key
              ? `<button class="copy-btn" onclick="copyFieldValue(this, ${item.id}, 'secret_key', '2FA秘钥')">2FA</button>`
              : ""
          }
          <button class="btn-browser" onclick="onClickLaunchBrowser(${item.id}, this)" title="打开账号专属浏览器">
            🌐 启动浏览器
          </button>
        </div>
      </div>

      ${drawerHtml}

      <!-- Line 4: Dedicated Footer (Device Code / Note & Meta Info + Tools) -->
      <div class="u-footer">
        <div class="u-meta-left">
          ${deviceChipHtml}
          ${metaLeft ? `<span class="u-meta-item">${escapeHtml(metaLeft)}</span>` : ""}
        </div>

        <div class="u-card-tools">
          <button class="tool-link" onclick="openQuickCredModal(${item.id})" title="4步安全改密、养号进度与历史凭据">🛡️ 养护</button>
          ${item.account_type === "域名邮箱" ? "" : `<button class="tool-link" onclick="openLoginTools(${item.id})">登录工具</button>`}
          ${
            item.account_type === "Google"
              ? `<button class="tool-link" onclick="openRtOAuthModal(${item.id})" title="Antigravity RT 探测与 OAuth 授权">🛸 RT</button>`
              : ""
          }
          ${
            (item.account_type === "ChatGPT" || item.account_type === "Claude") && item.has_ai_cred
              ? `<button class="tool-link" onclick="onClickProbeAiAccount(${item.id})" title="检测 API Key / Session Token 活性">🩺 测活</button>`
              : ""
          }
          ${
            item.account_type === "域名邮箱"
              ? `<button class="tool-link danger" onclick="confirmResetDomainEmailNodes(${item.id})" title="重置该邮箱绑定的代理节点与收码记录">重置节点</button>`
              : ""
          }
          ${
            state.viewMode === "compact"
              ? `<button class="tool-link" onclick="toggleCardExpand(${item.id})">${showDrawer ? "收起 ▴" : "详情 ▾"}</button>`
              : ""
          }
          <button class="tool-link" onclick="copyFullFormattedLine(${item.id})" title="按 ---- 格式复制整行">整串</button>
          <button class="tool-link" onclick="openEditModal(${item.id})">编辑</button>
          <button class="tool-link danger" onclick="confirmDeleteAccount(${item.id})">删除</button>
        </div>
      </div>
    </article>
  `;
}

function renderTableView(remaining, list = state.accounts) {
  const rows = list
    .map((item, idx) => {
      const showAcc = isRevealed(item.id, "account");
      const displayAcc = showAcc ? item.account : item.masked_account;
      const hasTotp = item.secret_key && item.totp && item.totp.valid;
      const totpFormatted = hasTotp ? formatTotpCode(item.totp.code) : "-";
      const usage = item.usage || { status: "active", status_label: "使用中", duration_text: "使用中" };
      const hMeta = getAccountHealthMeta(item);
      const warmInfo = getWarmingInfo(item);
      const agMeta = getAntigravityMeta(item.antigravity_tier || "无");
      const devMeta = getDeviceMeta(item.device_tag || "主机");
      const devAttr = (state.attributes || []).find((a) => a.attr_key === "device_tag");
      const showDevBadge = !devAttr || isAttributeApplicable(devAttr, item.account_type);
      const agAttr = (state.attributes || []).find((a) => a.attr_key === "antigravity_tier");
      const showAgBadge = !agAttr || isAttributeApplicable(agAttr, item.account_type);
      const isPwdModified = !!item.password_modified;
      const is2faModified = !!item.twofa_modified;
      const fpInfo = getFpSummary(item);
      const isClaude = item.account_type === "Claude";
      const isUnboundClaude = isClaude && !fpInfo.proxyNode && !(item.fp_config && item.fp_config.proxy);
      let nodePillText = fpInfo.proxyNode
        ? `${fpInfo.locked ? "🔒" : "🔓"} ${fpInfo.proxyNode}`
        : (isClaude ? "⚠️ 未绑定代理" : "🔓 绑定节点");
      if (item.account_type === "域名邮箱" && fpInfo.proxyNode) {
        const c = (state.nodeCodeStats && state.nodeCodeStats[fpInfo.proxyNode]) || 0;
        nodePillText = `${fpInfo.locked ? "🔒" : "🔓"} ${fpInfo.proxyNode} (${c}/2 已收码)`;
      }

      const healthBadgeHtml = `
        <div class="u-health-dropdown" data-acc-id="${item.id}">
          <button class="u-health-btn health-${hMeta.key}" onclick="toggleHealthMenu(event, ${item.id})" title="快捷维护可用状态">
            <span class="badge-dot ${hMeta.dotClass}"></span>
            <span class="health-btn-label">${escapeHtml(hMeta.label)}</span>
            <span class="health-arrow">▾</span>
          </button>
          <div class="u-health-popover" id="health-menu-${item.id}" style="display: none;">
            <div class="popover-title">维护可用状态</div>
            <button class="health-opt ${hMeta.key === 'available' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '可用')">
              <span class="badge-dot dot-available"></span><span>${item.account_type === "Google" ? "已确认登录" : "可用"}</span>
            </button>
            <button class="health-opt ${hMeta.key === 'banned' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '封号')">
              <span class="badge-dot dot-banned"></span><span>封号</span>
            </button>
            <button class="health-opt ${hMeta.key === 'captcha' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '人机')">
              <span class="badge-dot dot-captcha"></span><span>人机</span>
            </button>
            <button class="health-opt ${hMeta.key === 'sms' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '短信')">
              <span class="badge-dot dot-sms"></span><span>短信</span>
            </button>
            <button class="health-opt ${hMeta.key === 'verify' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '验证')">
              <span class="badge-dot dot-verify"></span><span>验证</span>
            </button>
            <button class="health-opt ${hMeta.key === 'unavailable' ? 'active' : ''}" onclick="onQuickSelectHealth(${item.id}, '不可用')">
              <span class="badge-dot dot-unavailable"></span><span>不可用</span>
            </button>
          </div>
        </div>
      `;

      const agBadgeTableHtml = `
        <div class="u-ag-dropdown" data-acc-id="${item.id}">
          <button class="u-ag-btn ag-${agMeta.key}" onclick="toggleAntigravityMenu(event, ${item.id})" title="快捷维护 Antigravity 标记">
            <span class="badge-dot ${agMeta.dotClass}"></span>
            <span class="ag-btn-label">${escapeHtml(agMeta.badgeText)}</span>
            <span class="health-arrow">▾</span>
          </button>
          <div class="u-ag-popover" id="ag-menu-${item.id}" style="display: none;">
            <div class="popover-title">Antigravity 标记</div>
            <button class="ag-opt opt-none ${agMeta.key === 'none' ? 'active' : ''}" onclick="onQuickSelectAntigravity(${item.id}, '无')">
              <span class="badge-dot dot-ag-none"></span><span>无</span>
            </button>
            <button class="ag-opt opt-blue ${agMeta.key === 'blue' ? 'active' : ''}" onclick="onQuickSelectAntigravity(${item.id}, '蓝色Pro')">
              <span class="badge-dot dot-ag-blue"></span><span>💎 Pro</span>
            </button>
            <button class="ag-opt opt-gold ${agMeta.key === 'gold' ? 'active' : ''}" onclick="onQuickSelectAntigravity(${item.id}, '金色Pro')">
              <span class="badge-dot dot-ag-gold"></span><span>👑 Pro</span>
            </button>
          </div>
        </div>
      `;

      const devBadgeTableHtml = `
        <div class="u-device-dropdown" data-acc-id="${item.id}">
          <button class="u-device-btn dev-${devMeta.key}" onclick="toggleDeviceMenu(event, ${item.id})" title="快捷维护设备标记">
            <span>${devMeta.icon}</span>
            <span>${escapeHtml(devMeta.label)}</span>
            <span class="health-arrow">▾</span>
          </button>
          <div class="u-device-popover" id="dev-menu-${item.id}" style="display: none;">
            <div class="popover-title">标记设备</div>
            <button class="device-opt ${devMeta.label === '主机' ? 'active' : ''}" onclick="onQuickSelectDevice(${item.id}, '主机')">
              <span>🖥️</span><span>主机</span>
            </button>
            <button class="device-opt ${devMeta.label === 'iPhone' ? 'active' : ''}" onclick="onQuickSelectDevice(${item.id}, 'iPhone')">
              <span>📱</span><span>iPhone</span>
            </button>
            <button class="device-opt ${devMeta.label === 'Pad' ? 'active' : ''}" onclick="onQuickSelectDevice(${item.id}, 'Pad')">
              <span>📱</span><span>Pad</span>
            </button>
          </div>
        </div>
      `;

      const deviceChipHtml = item.note
        ? `<span class="u-device-chip tb-device-chip" title="所在设备/备注: ${escapeHtml(item.note)}">
            <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect>
              <line x1="8" y1="21" x2="16" y2="21"></line>
              <line x1="12" y1="17" x2="12" y2="21"></line>
            </svg>
            <span class="device-text">${escapeHtml(item.note)}</span>
          </span>`
        : "";

      return `
        <tr class="ag-row-${showAgBadge ? agMeta.key : "none"}">
          <td>
            <div class="tb-col-platform">
              ${renderWorkBuddyCheckbox(item)}
              <span class="tb-idx">${idx + 1}</span>
              <span class="u-platform ${escapeHtml(item.account_type)}">${escapeHtml(item.account_type)}</span>
            </div>
          </td>
          <td>
            <div class="tb-col-account">
              <div class="tb-acc-row1">
                <span class="u-fp-badge" title="${escapeHtml(fpInfo.tooltip)}">${escapeHtml(fpInfo.code)}</span>
                <span class="u-email" onclick="copyFieldValue(null, ${item.id}, 'account', '账号')" title="${escapeHtml(item.account)}">${escapeHtml(displayAcc)}</span>
                <button class="u-peek-btn" onclick="toggleReveal(${item.id}, 'account')">${showAcc ? "隐" : "明"}</button>
              </div>
              <div class="tb-acc-row2">
                <button class="u-node-pill ${fpInfo.locked ? "locked" : ""} ${isUnboundClaude ? "unbound-warn" : ""}" onclick="openNodeModal(${item.id})" title="${escapeHtml(fpInfo.proxyNode || '直连')}">
                  ${escapeHtml(nodePillText)}
                </button>
                ${deviceChipHtml}
              </div>
            </div>
          </td>
          <td>
            <div class="tb-col-status">
              ${renderWorkBuddyItemBadges(item)}
              ${showDevBadge ? devBadgeTableHtml : ""}
              ${healthBadgeHtml}
            </div>
          </td>
          <td>
            <div class="tb-col-security">
              <div class="tb-sec-flags">
                <button class="sec-badge sec-${escapeHtml((item.security || {}).status || "unmodified")}" onclick="openQuickCredModal(${item.id})" title="点击打开4步安全加固与历史凭据">${escapeHtml((item.security || {}).label || "⚠️ 未改密")}</button>
                <button class="u-2fa-flag ${isPwdModified ? "modified" : "unmodified"}" onclick="toggleCardPasswordModified(${item.id}, ${isPwdModified ? 0 : 1})" title="密码修改状态">
                  ${isPwdModified ? "🔑 改✓" : "🔑 初始"}
                </button>
                ${
                  item.secret_key
                    ? `<button class="u-2fa-flag ${is2faModified ? "modified" : "unmodified"}" onclick="toggleCardTwofaModified(${item.id}, ${is2faModified ? 0 : 1})" title="2FA修改状态">
                        ${is2faModified ? "🛡️ 改✓" : "🛡️ 初始"}
                      </button>`
                    : `<span class="u-2fa-flag none">🛡️ 无2FA</span>`
                }
              </div>
              <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap;">
                ${showAgBadge ? agBadgeTableHtml : ""}
                <button class="warm-badge warm-${escapeHtml(warmInfo.phase)}" onclick="openQuickCredModal(${item.id})" title="${escapeHtml(warmInfo.hint || warmInfo.label)}">🌱 ${escapeHtml(warmInfo.label)}</button>
                <span class="u-usage-badge ${usage.status}">${escapeHtml(usage.duration_text)}</span>
              </div>
            </div>
          </td>
          <td>
            ${
              item.account_type === "域名邮箱"
                ? (item.custom_fields && item.custom_fields.latest_code
                    ? `<div class="tb-col-totp tb-mail-code-cell">
                        <span class="tb-mail-code-val" onclick="copyTotpCode(null, ${item.id})" title="点击复制最新邮件验证码: ${escapeHtml(item.custom_fields.latest_code)}">${escapeHtml(item.custom_fields.latest_code)}</span>
                        <button type="button" class="mail-code-refresh" aria-label="刷新此邮箱验证码" onclick="event.stopPropagation(); refreshSingleEmailCode(${item.id}, this)">刷新</button>
                      </div>`
                    : `<div class="tb-col-totp tb-mail-code-cell">
                        <span class="tb-mail-code-pending" onclick="copyTotpCode(null, ${item.id})" title="点击立即连接邮箱查信">暂无 (查信)</span>
                        <button type="button" class="mail-code-refresh" aria-label="刷新此邮箱验证码" onclick="event.stopPropagation(); refreshSingleEmailCode(${item.id}, this)">刷新</button>
                      </div>`
                  )
                : `<div class="tb-col-totp">
                    <span class="u-totp-code" id="totp-code-${item.id}" onclick="copyTotpCode(null, ${item.id})" title="点击复制动态码">${totpFormatted}</span>
                    ${hasTotp ? `<span class="u-totp-sec"><span class="card-rem-sec">${remaining}</span>s</span>` : ""}
                  </div>`
            }
          </td>
          <td>
            <div class="u-copy-group tb-copy-group">
              <button class="copy-btn" onclick="copyFieldValue(this, ${item.id}, 'account', '账号')">账号</button>
              <button class="copy-btn" onclick="copyFieldValue(this, ${item.id}, 'password', '密码')">密码</button>
              ${item.secret_key ? `<button class="copy-btn" onclick="copyFieldValue(this, ${item.id}, 'secret_key', '2FA秘钥')">2FA</button>` : ""}
            </div>
          </td>
          <td>
            <div class="tb-col-actions">
              <button class="btn-browser tb-btn-browser" onclick="onClickLaunchBrowser(${item.id}, this)" title="打开账号专属浏览器">🌐 启动</button>
              <div class="u-card-tools tb-tools">
                <button class="tool-link" onclick="openQuickCredModal(${item.id})" title="4步安全改密与养号进度">养护</button>
                ${item.account_type === "域名邮箱" ? "" : `<button class="tool-link" onclick="openLoginTools(${item.id})">登录工具</button>`}
                ${
                  item.account_type === "Google"
                    ? `<button class="tool-link" onclick="openRtOAuthModal(${item.id})" title="RT 诊断与 OAuth 授权">RT</button>`
                    : ""
                }
                ${
                  (item.account_type === "ChatGPT" || item.account_type === "Claude") && item.has_ai_cred
                    ? `<button class="tool-link" onclick="onClickProbeAiAccount(${item.id})" title="测活 API/Session">测活</button>`
                    : ""
                }
                ${
                  item.account_type === "域名邮箱"
                    ? `<button class="tool-link danger" onclick="confirmResetDomainEmailNodes(${item.id})" title="重置该邮箱绑定的代理节点">重置节点</button>`
                    : ""
                }
                <button class="tool-link" onclick="openEditModal(${item.id})">编辑</button>
                <button class="tool-link danger" onclick="confirmDeleteAccount(${item.id})">删除</button>
              </div>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");

  return `
    <table class="u-table">
      <thead>
        <tr>
          <th style="width: 9%; min-width: 72px;"># 平台</th>
          <th style="width: 27%; min-width: 195px;">账号 / 代理节点</th>
          <th style="width: 13%; min-width: 100px;">设备 / 状态</th>
          <th style="width: 16%; min-width: 120px;">安全 / Pro</th>
          <th style="width: 11%; min-width: 80px;">2FA动态码</th>
          <th style="width: 11%; min-width: 85px;">快捷复制</th>
          <th style="width: 13%; min-width: 110px;">启动 / 操作</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

// ============================================================
// PROXY NODE SELECTOR & NODE LOCK MODAL
// ============================================================
async function onClickLaunchBrowser(accountId, btnEl = null) {
  const item = state.accounts.find((a) => a.id === accountId);
  if (!item) return;
  const fp = item.fp_config || {};
  if (item.account_type === "域名邮箱" || item.account_type === "Google") {
    await executeLaunchBrowser(accountId, btnEl);
    return;
  }
  // If account hasn't locked/bound a node yet, open the Node Selector & Lock modal first!
  if (!fp.proxy_node && !fp.node_locked) {
    await openNodeModal(accountId);
    return;
  }
  await executeLaunchBrowser(accountId, btnEl);
}

async function executeLaunchBrowser(accountId, btnEl = null, targetUrl = "") {
  const origHtml = btnEl ? btnEl.innerHTML : "";
  const item = state.accounts.find((a) => a.id === accountId);
  if (btnEl) {
    btnEl.classList.add("loading");
    btnEl.disabled = true;
    btnEl.innerHTML = "<span>🚀 正在启动...</span>";
  }
  showToast("正在打开浏览器...", 2800);

  try {
    const res = await callApi("launch_browser", accountId, targetUrl);
    if (res && res.ok) {
      if (res.mailbox) {
        showToast("已在默认浏览器打开域名邮箱");
        return;
      }
      if (res.account) {
        const idx = state.accounts.findIndex((a) => a.id === accountId);
        if (idx >= 0) state.accounts[idx] = res.account;
        renderAccounts();
      }
      showBrowserLaunchResult(accountId, res, res.account || item);
      if (item?.account_type === "域名邮箱") await loadDashboard();
    } else {
      showToast(res?.error || "启动浏览器失败");
    }
  } catch (err) {
    showToast("启动浏览器异常: " + err.message);
  } finally {
    if (btnEl) {
      btnEl.classList.remove("loading");
      btnEl.disabled = false;
      btnEl.innerHTML = origHtml;
    }
  }
}

function showBrowserLaunchResult(accountId, result, account = state.accounts.find((item) => item.id === accountId)) {
  if (account && account.account_type !== "域名邮箱") openLoginTools(accountId);
  if (result.profile_cleanup_pending) {
    showToast("请关闭该账号的浏览器窗口后重新启动，以处理旧扩展。");
  } else {
    showToast("已请求打开浏览器。");
  }
}

function openLoginTools(accountId) {
  state.loginToolsAccountId = accountId;
  renderLoginTools();
}

async function showFloatingLoginTools(accountId = state.loginToolsAccountId) {
  if (!accountId) return;
  let error = "无法打开悬浮登录工具，可在 KeyVault 主窗口复制。";
  try {
    const result = await callApi("show_login_tools", accountId);
    if (result?.ok) {
      showToast("切回该账号的浏览器即可看到登录工具条。");
      return;
    }
    error = result?.error || error;
  } catch (_) {
    // The dashboard toolbar remains available when the native panel cannot open.
  }
  showToast(error);
}

function closeLoginTools() {
  state.loginToolsAccountId = null;
  renderLoginTools();
}

function getLoginToolsAccount() {
  return state.accounts.find((account) =>
    account.id === state.loginToolsAccountId && account.account_type !== "域名邮箱"
  );
}

function renderLoginTools() {
  const toolbar = document.getElementById("login-tools");
  const account = getLoginToolsAccount();
  toolbar.hidden = !account;
  document.getElementById("login-tool-account").textContent = account?.account || "";
  document.getElementById("login-tool-copy-password").disabled = !account?.password;
  renderLoginToolsTotp(account);
}

function renderLoginToolsTotp(account, remaining) {
  const code = account?.totp?.valid ? account.totp.code : "";
  document.getElementById("login-tool-totp").textContent = code ? formatTotpCode(code) : "—";
  document.getElementById("login-tool-totp-time").textContent = code
    ? `${remaining ?? document.getElementById("global-remaining").textContent}s`
    : account ? "无动态验证码" : "";
  document.getElementById("login-tool-copy-totp").disabled = !code;
}

async function copyLoginToolValue(button, field) {
  const account = getLoginToolsAccount();
  if (!account) return;
  try {
    let value = account[field];
    if (field === "totp") {
      const response = await callApi("preview_totp", account.secret_key);
      if (getLoginToolsAccount()?.id !== account.id) return;
      value = response?.totp?.valid ? response.totp.code : "";
      account.totp = response?.totp;
      renderLoginToolsTotp(account, response?.totp?.remaining);
    }
    if (!await writeClipboard(value)) {
      showToast("没有可复制的内容，或剪贴板不可用。");
      return;
    }
    flashCopyButton(button);
    const labels = { account: "账号", password: "密码", totp: "验证码" };
    showToast(`已复制${labels[field]}`);
  } catch (_) {
    showToast("复制失败，请重试。");
  }
}

async function openNodeModal(accountId) {
  const item = state.accounts.find((a) => a.id === accountId);
  if (!item) return;
  state.nodeModalAccountId = accountId;

  const fp = item.fp_config || {};
  document.getElementById("node-modal-title").textContent = `节点锁定配置 · [${fp.profile_code || "FP"}] ${item.masked_account}`;
  document.getElementById("node-modal").style.display = "flex";

  // Claude-specific guidance banner
  const claudeHintEl = document.getElementById("node-claude-hint");
  if (claudeHintEl) {
    claudeHintEl.style.display = item.account_type === "Claude" ? "flex" : "none";
  }

  // Loading state
  const loadingEl = document.getElementById("node-modal-loading");
  const contentEl = document.getElementById("node-modal-main-content");
  const subEl = document.getElementById("node-modal-sub");
  const btnSaveOnly = document.getElementById("btn-save-node-only");
  const btnSaveLaunch = document.getElementById("btn-save-and-launch");

  if (loadingEl) loadingEl.style.display = "flex";
  if (contentEl) contentEl.style.display = "none";
  if (btnSaveOnly) btnSaveOnly.disabled = true;
  if (btnSaveLaunch) btnSaveLaunch.disabled = true;
  subEl.textContent = "正在实时探测本机代理端口与节点列表，请稍候...";

  try {
    state.proxyEnv = await callApi("get_proxy_nodes", item.account_type);
  } catch (e) {
    state.proxyEnv = { client_name: "未检测到代理", controller_online: false, ports: [], nodes: [] };
  }

  if (loadingEl) loadingEl.style.display = "none";
  if (contentEl) contentEl.style.display = "block";
  if (btnSaveOnly) btnSaveOnly.disabled = false;
  if (btnSaveLaunch) btnSaveLaunch.disabled = false;

  const env = state.proxyEnv;
  const activeHint = env.active_node ? ` · 本机当前在用:【${env.active_node}】` : "";
  subEl.textContent = `已探测本机代理环境：${env.client_name} (${env.controller_online ? "🟢 控制接口在线" : "⚪ 端口/配置模式"}) · 共发现 ${(env.nodes || []).length} 个可用节点${activeHint}`;

  // Populate Ports Dropdown
  const portSelect = document.getElementById("node-port-select");
  const customInput = document.getElementById("node-custom-proxy");
  const ports = env.ports && env.ports.length
    ? env.ports
    : [{ url: "http://127.0.0.1:7890", label: "127.0.0.1:7890 (通用默认代理端口)" }];

  state.selectedProxyUrl = fp.proxy ? fp.proxy : ports[0].url;

  // Auto select active node by default if not locked
  if (fp.node_locked && fp.proxy_node) {
    state.selectedNodeName = fp.proxy_node;
  } else if (fp.proxy_node) {
    state.selectedNodeName = fp.proxy_node;
  } else if (env.active_node) {
    state.selectedNodeName = env.active_node;
  } else {
    state.selectedNodeName = "";
  }

  // Auto switch tab to the active node's region if present
  if (state.selectedNodeName) {
    const matchedNode = (env.nodes || []).find((n) => n.name === state.selectedNodeName);
    if (matchedNode && matchedNode.region) {
      state.selectedRegion = matchedNode.region;
    } else {
      state.selectedRegion = "全部";
    }
  } else {
    state.selectedRegion = "全部";
  }

  // Claude accounts without any proxy: pre-select the recommended sidecar/Clash port and a US residential node
  if (item.account_type === "Claude" && !fp.proxy && !fp.proxy_node) {
    const prefPort = ports.find((p) => p.url && (p.url.includes("17899") || p.url.includes("7897")));
    if (prefPort) {
      state.selectedProxyUrl = prefPort.url;
    }
    const recNode =
      (env.nodes || []).find((n) => /miyaip|isp|住宅|家宽/i.test(n.name)) ||
      (env.nodes || []).find((n) => n.region === "美国" || n.region === "US" || /us/i.test(n.name));
    if (recNode) {
      state.selectedNodeName = recNode.name;
      state.selectedRegion = recNode.region || "全部";
    }
  }

  const portOptionsHtml = ports
    .map(
      (p) =>
        `<option value="${escapeHtml(p.url)}" ${p.url === state.selectedProxyUrl ? "selected" : ""}>${escapeHtml(p.label)}</option>`
    )
    .join("");
  portSelect.innerHTML = portOptionsHtml + `<option value="__custom__" ${!ports.some((p) => p.url === state.selectedProxyUrl) ? "selected" : ""}>自定义代理地址 / 直连...</option>`;
  if (customInput) {
    customInput.value = state.selectedProxyUrl;
  }

  portSelect.onchange = (e) => {
    if (e.target.value !== "__custom__") {
      state.selectedProxyUrl = e.target.value;
      if (customInput) customInput.value = state.selectedProxyUrl;
    } else if (customInput) {
      customInput.focus();
    }
  };
  syncCustomSelect(portSelect);

  renderNodeModalLockState(item);
  renderNodeRegionTabs();
  renderNodeGrid(!!fp.node_locked);

  setTimeout(() => {
    const selEl = document.querySelector(".node-item.selected");
    if (selEl) {
      selEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, 100);
}

function onCustomProxyUrlInput(val) {
  state.selectedProxyUrl = (val || "").trim();
}

function renderNodeModalLockState(item) {
  const fp = item.fp_config || {};
  const isLocked = !!fp.node_locked;
  const banner = document.getElementById("node-locked-banner");
  const portSelect = document.getElementById("node-port-select");
  const customInput = document.getElementById("node-custom-proxy");
  const btnSaveOnly = document.getElementById("btn-save-node-only");
  const btnSaveLaunch = document.getElementById("btn-save-and-launch");

  if (isLocked) {
    banner.style.display = "flex";
    document.getElementById("locked-node-title").textContent = `当前账号已锁定：${fp.proxy_node || "仅固定代理端口"} (${fp.proxy || "直连"})`;
    portSelect.disabled = true;
    if (customInput) customInput.disabled = true;
    btnSaveOnly.style.display = "none";
    btnSaveLaunch.textContent = "🚀 以当前锁定环境直接启动浏览器";
  } else {
    banner.style.display = "none";
    portSelect.disabled = false;
    if (customInput) customInput.disabled = false;
    btnSaveOnly.style.display = "inline-flex";
    btnSaveLaunch.textContent = "🚀 锁定此配置并启动浏览器";
  }
  syncCustomSelect(portSelect);
}

function renderNodeRegionTabs() {
  const nodes = (state.proxyEnv && state.proxyEnv.nodes) || [];
  const regions = ["全部"];
  nodes.forEach((n) => {
    if (n.region && !regions.includes(n.region)) regions.push(n.region);
  });

  const tabsEl = document.getElementById("node-region-tabs");
  if (!tabsEl) return;
  if (!tabsEl._delegated) {
    tabsEl._delegated = true;
    tabsEl.addEventListener("click", (e) => {
      const btn = e.target.closest(".region-tab[data-region]");
      if (btn) selectNodeRegion(btn.getAttribute("data-region") || "全部");
    });
  }
  tabsEl.innerHTML = regions
    .map(
      (r) =>
        `<button type="button" class="region-tab ${state.selectedRegion === r ? "active" : ""}" data-region="${escapeHtml(r)}">${escapeHtml(r)}</button>`
    )
    .join("");
}

function selectNodeRegion(region) {
  state.selectedRegion = region;
  const item = state.accounts.find((a) => a.id === state.nodeModalAccountId);
  const isLocked = !!(item && item.fp_config && item.fp_config.node_locked);
  renderNodeRegionTabs();
  renderNodeGrid(isLocked);
}

function renderNodeGrid(isLocked) {
  const nodes = (state.proxyEnv && state.proxyEnv.nodes) || [];
  const activeNodeName = (state.proxyEnv && state.proxyEnv.active_node) || "";
  const filtered = state.selectedRegion === "全部" ? nodes : nodes.filter((n) => n.region === state.selectedRegion);
  const gridEl = document.getElementById("node-grid-container");
  if (!gridEl) return;
  if (!gridEl._delegated) {
    gridEl._delegated = true;
    gridEl.addEventListener("click", (e) => {
      const nodeEl = e.target.closest(".node-item[data-node-name]");
      if (!nodeEl) return;
      onSelectNodeItem(
        nodeEl.getAttribute("data-node-name") || "",
        nodeEl.getAttribute("data-node-locked") === "1"
      );
    });
  }

  const curItem = state.accounts.find((a) => a.id === state.nodeModalAccountId);
  const isClaudeAcc = curItem && curItem.account_type === "Claude";

  const portOnlySelected = state.selectedNodeName === "";
  const portOnlyCard = `
    <div class="node-item ${portOnlySelected ? "selected" : ""} ${isLocked ? "disabled" : ""}" data-node-name="" data-node-locked="${isLocked ? "1" : "0"}">
      <span>🌐 仅绑定代理端口 (不切换特定节点 / 兼容 v2rayN·Sing-box)</span>
    </div>
  `;

  if (!filtered.length) {
    gridEl.innerHTML =
      portOnlyCard +
      `<div style="color:var(--text-secondary);font-size:12px;padding:12px;">未探测到该分区下的节点，可直接锁定上方代理端口使用</div>`;
    return;
  }

  gridEl.innerHTML =
    (state.selectedRegion === "全部" ? portOnlyCard : "") +
    filtered
      .map((n) => {
        const isSel = n.name === state.selectedNodeName;
        const isLiveActive = n.name === activeNodeName;
        const delayHtml = n.delay ? `<span class="node-delay">${n.delay}ms</span>` : "";
        const activeBadge = isLiveActive ? `<span class="node-item-badge" title="本机当前正在生效的代理节点">🟢 当前在用</span>` : "";
        const isRecommend = isClaudeAcc && /miyaip|isp|住宅|家宽/i.test(n.name);
        const recBadge = isRecommend ? `<span class="node-badge-recommend">🌟 推荐住宅</span>` : "";
        return `
          <div class="node-item ${isSel ? "selected" : ""} ${isLiveActive ? "is-active-now" : ""} ${isLocked ? "disabled" : ""}" data-node-name="${escapeHtml(n.name)}" data-node-locked="${isLocked ? "1" : "0"}">
            <span style="display:flex;align-items:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
              ${escapeHtml(n.name)}
              ${activeBadge}
              ${recBadge}
            </span>
            ${delayHtml}
          </div>
        `;
      })
      .join("");
}

function onSelectNodeItem(nodeName, isLocked) {
  if (isLocked) {
    showToast("🔒 该账号节点已锁定，请先点击上方「解锁节点」后再变更！");
    return;
  }
  state.selectedNodeName = nodeName;
  renderNodeGrid(false);
}

async function unlockCurrentAccountNode() {
  if (!state.nodeModalAccountId) return;
  try {
    const res = await callApi("set_node_lock", state.nodeModalAccountId, false);
    if (res && res.ok) {
      showToast("🔓 已解锁该账号节点，现在可以重新选择新节点并锁定");
      await loadDashboard();
      const updatedItem = state.accounts.find((a) => a.id === state.nodeModalAccountId);
      if (updatedItem) {
        renderNodeModalLockState(updatedItem);
        renderNodeGrid(false);
      }
    }
  } catch (err) {
    showToast("解锁失败");
  }
}

async function saveAndLockNode(andLaunch = false) {
  const accId = state.nodeModalAccountId;
  if (!accId) return;
  const item = state.accounts.find((a) => a.id === accId);
  if (!item) return;

  const btn = andLaunch ? document.getElementById("btn-save-and-launch") : document.getElementById("btn-save-node-only");
  if (btn) {
    btn.classList.add("loading");
    btn.disabled = true;
  }

  // If already locked and user clicked "Launch with locked node"
  if (item.fp_config && item.fp_config.node_locked) {
    closeNodeModal();
    if (andLaunch) {
      await executeLaunchBrowser(accId);
    }
    return;
  }

  if (!state.selectedNodeName && !state.selectedProxyUrl) {
    if (btn) {
      btn.classList.remove("loading");
      btn.disabled = false;
    }
    showToast("请至少选择一个代理端口或节点");
    return;
  }

  try {
    const res = await callApi(
      "bind_proxy_node",
      accId,
      state.selectedProxyUrl,
      state.selectedNodeName || "",
      true
    );
    if (res && res.ok) {
      showToast(`🔒 已为该账号锁定代理环境: ${state.selectedNodeName || state.selectedProxyUrl || "直连"}`);
      closeNodeModal();
      await loadDashboard();
      if (andLaunch) {
        await executeLaunchBrowser(accId);
      }
    } else {
      showToast(res.error || "锁定节点失败");
    }
  } catch (err) {
    showToast("保存节点异常: " + err.message);
  } finally {
    if (btn) {
      btn.classList.remove("loading");
      btn.disabled = false;
    }
  }
}

function closeNodeModal() {
  document.getElementById("node-modal").style.display = "none";
}

async function toggleCardTwofaModified(accountId, nextVal) {
  if (state.accounts.find((account) => account.id === accountId)?.account_type === "Google") {
    return openQuickCredModal(accountId);
  }
  try {
    const res = await callApi("toggle_twofa_modified", accountId, nextVal);
    if (res && res.ok) {
      showToast(nextVal ? "✓ 已标记为「2FA已修改」" : "已切换为「初始2FA」");
      await loadDashboard();
    }
  } catch (err) {
    showToast("切换状态失败");
  }
}

async function toggleCardPasswordModified(accountId, nextVal) {
  if (state.accounts.find((account) => account.id === accountId)?.account_type === "Google") {
    return openQuickCredModal(accountId);
  }
  try {
    const res = await callApi("toggle_password_modified", accountId, nextVal);
    if (res && res.ok) {
      showToast(nextVal ? "✓ 已标记为「密码已修改」" : "已切换为「初始密码」");
      await loadDashboard();
    }
  } catch (err) {
    showToast("切换状态失败");
  }
}

// Zero-Idle-CPU TOTP Countdown Timer
function startZeroOverheadTotpTicker() {
  const circumference = 2 * Math.PI * 9;
  const ringEl = document.getElementById("global-ring");
  const remEl = document.getElementById("global-remaining");

  if (totpTickerTimer) clearInterval(totpTickerTimer);
  totpTickerTimer = setInterval(async () => {
    const totpOffsetSec = Number(state.settings?.totp_offset_seconds || 0);
    const nowSec = Math.floor(Date.now() / 1000) + totpOffsetSec;
    const remaining = 30 - (((nowSec % 30) + 30) % 30);
    const currentSlot = Math.floor(nowSec / 30);

    if (remEl) remEl.textContent = remaining;
    if (ringEl) {
      const offset = circumference * (1 - remaining / 30);
      ringEl.style.strokeDashoffset = offset.toFixed(2);
      ringEl.classList.toggle("urgent", remaining <= 5);
    }

    document.querySelectorAll(".card-rem-sec").forEach((el) => {
      el.textContent = remaining;
    });

    if (currentSlot !== state.lastTotpSlot) {
      state.lastTotpSlot = currentSlot;
      try {
        const res = await callApi("refresh_totp");
        if (res && res.codes) {
          state.accounts.forEach((acc) => {
            if (acc.account_type === "域名邮箱") return;
            const updatedTotp = res.codes[String(acc.id)];
            if (updatedTotp) {
              acc.totp = updatedTotp;
              const codeEl = document.getElementById(`totp-code-${acc.id}`);
              if (codeEl && updatedTotp.valid) {
                codeEl.textContent = formatTotpCode(updatedTotp.code);
              }
            }
          });
        }
      } catch (e) {
        console.error("Refresh TOTP error:", e);
      }
    }
    if (state.loginToolsAccountId !== null) {
      renderLoginToolsTotp(getLoginToolsAccount(), remaining);
    }
  }, 1000);
}

// ============================================================
// DOMAIN EMAIL VERIFICATION CODES, POLLING & NODE RESET
// ============================================================
let mailCodePollingTimer = null;
let mailCodePollingBusy = false;

function startMailCodePolling() {
  if (!state.autoMailCodePolling || state.filterType !== "域名邮箱") {
    stopMailCodePolling();
    return;
  }
  if (mailCodePollingTimer) return;
  mailCodePollingTimer = setInterval(async () => {
    if (!state.autoMailCodePolling || state.filterType !== "域名邮箱") {
      stopMailCodePolling();
      return;
    }
    await pollDomainEmailCodes(false);
  }, 4500);
}

function stopMailCodePolling() {
  if (mailCodePollingTimer) {
    clearInterval(mailCodePollingTimer);
    mailCodePollingTimer = null;
  }
}

function syncAutoMailPollButton() {
  const btn = document.getElementById("btn-toggle-auto-mail-poll");
  if (!btn) return;
  if (state.autoMailCodePolling) {
    btn.className = "btn btn-secondary";
    btn.textContent = "🟢 静默刷新: 开";
  } else {
    btn.className = "btn btn-ghost";
    btn.textContent = "⏸️ 静默刷新: 关";
  }
}

function toggleAutoMailCodePolling() {
  state.autoMailCodePolling = !state.autoMailCodePolling;
  syncAutoMailPollButton();
  if (state.autoMailCodePolling) {
    showToast("已开启后台静默刷新验证码");
    if (state.filterType === "域名邮箱") {
      startMailCodePolling();
      pollDomainEmailCodes(false);
    }
  } else {
    stopMailCodePolling();
    showToast("已关闭后台静默刷新验证码");
  }
}

async function pollDomainEmailCodes(isManual = false, targetAccountIds = null) {
  if (!isManual && mailCodePollingBusy) return { updatedCount: 0, newCodesFound: [], codes: {} };
  const acquiredBgLock = !isManual;
  if (acquiredBgLock) mailCodePollingBusy = true;
  try {
    const res = await callApi("get_domain_email_codes", targetAccountIds || null);
    if (res && res.ok) {
      let statsChanged = false;
      if (res.node_stats) {
        if (JSON.stringify(state.nodeCodeStats) !== JSON.stringify(res.node_stats)) {
          state.nodeCodeStats = res.node_stats;
          statsChanged = true;
        }
      }
      const codes = res.codes || {};
      let updatedCount = 0;
      const newCodesFound = [];

      for (const item of state.accounts) {
        const info = codes[item.id] || codes[String(item.id)];
        if (!info || !info.code) continue;

        if (!item.custom_fields) {
          item.custom_fields = {};
        }
        const oldCode = item.custom_fields.latest_code;
        if (oldCode !== info.code) {
          item.custom_fields.latest_code = info.code;
          item.custom_fields.code_received_at = info.received_at || Math.floor(Date.now() / 1000);
          item.custom_fields.code_subject = info.subject || "";
          item.custom_fields.has_received_code = true;
          updatedCount++;
          newCodesFound.push({ email: item.account, code: info.code });
        } else {
          if (info.received_at) item.custom_fields.code_received_at = info.received_at;
          if (info.subject) item.custom_fields.code_subject = info.subject;
        }
      }

      if (updatedCount > 0 || statsChanged) {
        renderAccounts();
        for (const nc of newCodesFound) {
          showToast(`✉️ 收到新验证码: ${nc.code}`);
        }
      } else if (isManual) {
        const codeKeys = Object.keys(codes);
        const targetMissing = Array.isArray(targetAccountIds) && targetAccountIds.length > 0
          && !targetAccountIds.some((id) => codes[id] || codes[String(id)]);
        if (codeKeys.length === 0 || targetMissing) {
          showToast("收件箱查信完成，暂无新验证码");
        }
      }
      return { updatedCount, newCodesFound, codes };
    } else if (isManual && res && res.error) {
      showToast("查信失败: " + res.error);
    }
  } catch (err) {
    if (isManual) {
      showToast("查信异常: " + err.message);
    }
  } finally {
    if (acquiredBgLock) mailCodePollingBusy = false;
  }
  return { updatedCount: 0, newCodesFound: [], codes: {} };
}

async function refreshDomainEmailCodes() {
  const btn = document.getElementById("btn-refresh-mail-codes");
  const origHtml = btn ? btn.innerHTML : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = "<span>🔄 查信中...</span>";
  }
  showToast("正在连接收件箱检查验证码...", 2000);
  try {
    const selectedIds = (typeof workbuddy !== "undefined" && workbuddy.selected && workbuddy.selected.size > 0)
      ? [...workbuddy.selected]
      : null;
    await pollDomainEmailCodes(true, selectedIds);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origHtml;
    }
  }
}

async function refreshSingleEmailCode(accountId, button = null) {
  const codeEl = document.getElementById(`totp-code-${accountId}`);
  const prevText = codeEl ? codeEl.textContent : "";
  const prevButtonText = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "查信中…";
  }
  showToast("正在检查该邮箱收件箱...", 2000);
  try {
    const result = await pollDomainEmailCodes(true, [accountId]);
    const info = result && result.codes
      ? (result.codes[accountId] || result.codes[String(accountId)])
      : null;
    if (info && info.code) {
      if (button) {
        showToast(`✓ 已刷新验证码: ${info.code}`);
      } else {
        await writeClipboard(String(info.code));
        showToast(`✓ 已获取并复制验证码: ${info.code}`);
      }
    } else if (codeEl && document.body.contains(codeEl)) {
      codeEl.textContent = prevText || "暂无 (查信)";
    }
  } catch (err) {
    if (codeEl && document.body.contains(codeEl)) {
      codeEl.textContent = prevText || "暂无 (查信)";
    }
    showToast("查信失败: " + err.message);
  } finally {
    if (button && document.body.contains(button)) {
      button.disabled = false;
      button.textContent = prevButtonText || "刷新";
    }
  }
}

async function confirmResetDomainEmailNodes(accountId = null) {
  const validId = typeof accountId === "number" ? accountId : null;
  const msg = "确定要重置代理节点绑定吗？这将清除绑定的节点与已收验证码计数，使节点可以重新从头分配。";
  if (!confirm(msg)) return;
  try {
    const res = await callApi("reset_domain_email_nodes", validId ? [validId] : null);
    if (res && res.ok) {
      showToast("已重置代理节点与收码记录");
      await loadDashboard();
    } else {
      showToast("重置失败: " + (res && res.error ? res.error : "未知错误"));
    }
  } catch (err) {
    showToast("重置异常: " + err.message);
  }
}

function formatTimeAgo(ts) {
  if (!ts) return "";
  let timeMs = typeof ts === "number" ? (ts < 1e11 ? ts * 1000 : ts) : Date.parse(ts);
  if (isNaN(timeMs)) return "";
  const diffSec = Math.max(0, Math.floor((Date.now() - timeMs) / 1000));
  if (diffSec < 60) return `${diffSec}秒前`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}分钟前`;
  const diffHours = Math.floor(diffMin / 60);
  if (diffHours < 24) return `${diffHours}小时前`;
  const diffDays = Math.floor(diffHours / 24);
  return `${diffDays}天前`;
}

// Copy Helpers
async function writeClipboard(text) {
  if (!text) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      return document.execCommand("copy");
    } finally {
      ta.remove();
    }
  }
}

async function copyText(text, label = "验证码") {
  if (!text) return;
  await writeClipboard(String(text));
  showToast(`✓ 已复制 ${label}: ${text}`);
}

async function copyFieldValue(btnEl, accountId, fieldName, labelText) {
  const item = state.accounts.find((a) => a.id === accountId);
  if (!item) return;
  let rawValue = item[fieldName] || "";
  if (fieldName === "secret_key" || fieldName === "initial_secret_key") {
    rawValue = normalizeSecretString(rawValue);
  }
  await writeClipboard(rawValue);
  flashCopyButton(btnEl);
  showToast(`✓ 已复制 ${labelText}`);
}

async function copyTotpCode(btnEl, accountId) {
  const item = state.accounts.find((a) => a.id === accountId);
  if (!item) return;
  if (item.account_type === "域名邮箱") {
    const code = item.custom_fields && item.custom_fields.latest_code;
    if (code) {
      await writeClipboard(String(code));
      flashCopyButton(btnEl);
      showToast(`✓ 已复制最新邮件验证码: ${code}`);
    } else {
      await refreshSingleEmailCode(item.id);
    }
    return;
  }
  if (!item.totp || !item.totp.code) return;
  await writeClipboard(item.totp.code);
  flashCopyButton(btnEl);
  showToast(`✓ 已复制 6 位动态验证码: ${item.totp.code}`);
}

async function copyFullFormattedLine(accountId) {
  const item = state.accounts.find((a) => a.id === accountId);
  if (!item) return;
  let line = "";
  if (item.account_type === "Google" || item.recovery_email || item.reg_year || item.reg_region) {
    const parts = [
      item.account,
      item.password,
      item.recovery_email,
      item.secret_key,
      item.reg_year,
      item.reg_region,
    ];
    while (parts.length && !parts[parts.length - 1]) parts.pop();
    line = parts.join("----");
  } else {
    const parts = [item.account, item.password];
    if (item.secret_key) parts.push(item.secret_key);
    line = parts.join("----");
  }
  await writeClipboard(line);
  showToast("✓ 已按标准 ---- 格式复制整条账号");
}

function flashCopyButton(btnEl) {
  if (!btnEl || !btnEl.classList.contains("copy-btn")) return;
  if (!btnEl.dataset.origText) {
    btnEl.dataset.origText = btnEl.textContent;
  }
  if (btnEl._copyTimer) {
    clearTimeout(btnEl._copyTimer);
  }
  btnEl.classList.add("copied");
  btnEl.textContent = "✓ 已复制";
  btnEl._copyTimer = setTimeout(() => {
    btnEl.classList.remove("copied");
    btnEl.textContent = btnEl.dataset.origText || btnEl.textContent;
    btnEl._copyTimer = null;
  }, 1100);
}

// Search & Filter

let searchDebounce = null;
function onSearchInput(val) {
  state.searchQuery = val;
  document.getElementById("btn-clear-search").style.display = val ? "block" : "none";
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    loadDashboard();
  }, 160);
}

function clearSearch() {
  const input = document.getElementById("search-input");
  input.value = "";
  onSearchInput("");
}

// Theme & View Mode
function applyTheme(theme, persist = true) {
  state.theme = theme;
  document.documentElement.setAttribute("data-theme", theme);
  document.body.classList.toggle("light-theme", theme === "light");
  if (persist) {
    callApi("save_settings", { theme }).catch(() => {});
  }
}

function toggleTheme() {
  const next = state.theme === "dark" ? "light" : "dark";
  applyTheme(next, true);
}

function applyViewMode(mode, persist = true) {
  state.viewMode = mode;
  document.getElementById("view-compact").classList.toggle("active", mode === "compact");
  document.getElementById("view-full").classList.toggle("active", mode === "full");
  document.getElementById("view-table").classList.toggle("active", mode === "table");
  renderAccounts();
  if (persist) {
    callApi("save_settings", { view_mode: mode }).catch(() => {});
  }
}

function setViewMode(mode) {
  applyViewMode(mode, true);
}

function fillTodayDeactivated() {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  document.getElementById("field-deactivated-date").value = `${y}-${m}-${d}`;
}

// Modal: Add / Edit + Express Smart Paste
let accountEditDynamicValues = null;

function openAddModal() {
  clearAccountEditHistory();
  accountEditDynamicValues = null;
  document.getElementById("modal-title").textContent = "添加账号 · 智能快递式识别";
  document.getElementById("smart-paste-section").style.display = "block";
  document.getElementById("batch-banner").style.display = "none";
  document.getElementById("smart-paste-input").value = "";
  resetFeedbackText();
  resetFormFields();

  const defType = (state.filterType && state.filterType !== "All") ? state.filterType : (state.settings?.default_account_type || "ChatGPT");
  state.importAccountType = (defType && defType !== "ChatGPT") ? defType : "";
  syncAccountTypeRadios(defType);
  renderAccountModalDynamicFields(defType, {
    health_status: "可用",
    antigravity_tier: "无",
    device_tag: state.settings?.default_device_tag || "主机",
  });

  document.getElementById("account-modal").style.display = "flex";
  setTimeout(() => document.getElementById("smart-paste-input").focus(), 60);
}

function openEditModal(accountId) {
  const item = state.accounts.find((a) => a.id === accountId);
  if (!item) return;
  document.getElementById("modal-title").textContent = "编辑账号信息";
  document.getElementById("smart-paste-section").style.display = "block";
  document.getElementById("batch-banner").style.display = "none";
  document.getElementById("smart-paste-input").value = "";
  resetFeedbackText();

  document.getElementById("field-id").value = item.id;
  state.importAccountType = (item.account_type && item.account_type !== "ChatGPT") ? item.account_type : "";
  syncAccountTypeRadios(item.account_type || "ChatGPT");
  renderAccountModalDynamicFields(item.account_type || "ChatGPT", {
    health_status: item.health_status,
    antigravity_tier: item.antigravity_tier,
    device_tag: item.device_tag,
    ...(item.custom_fields || {}),
  });
  accountEditDynamicValues = { accountType: item.account_type, values: getCurrentDynamicFormValues() };
  document.getElementById("field-password-modified").checked = !!item.password_modified;
  document.getElementById("field-twofa-modified").checked = !!item.twofa_modified;
  document.getElementById("field-account").value = item.account || "";
  const pwdEl = document.getElementById("field-password");
  pwdEl.value = item.password || "";
  pwdEl.oninput = () => {
    if (getRadioType() !== "Google" && item.password && pwdEl.value.trim() && pwdEl.value.trim() !== item.password) {
      document.getElementById("field-password-modified").checked = true;
    }
  };
  document.getElementById("field-secret").value = normalizeSecretString(item.secret_key || "");
  document.getElementById("field-initial-secret").value = normalizeSecretString(item.initial_secret_key || "");
  document.getElementById("field-purchase-date").value = item.purchase_date || "";
  document.getElementById("field-deactivated-date").value = item.deactivated_date || "";
  document.getElementById("field-recovery").value = item.recovery_email || "";
  document.getElementById("field-year").value = item.reg_year || "";
  document.getElementById("field-region").value = item.reg_region || "";
  document.getElementById("field-note").value = item.note || "";
  const recPwdEl = document.getElementById("field-recovery-password");
  if (recPwdEl) recPwdEl.value = (item.custom_fields && item.custom_fields.recovery_password) || "";
  const apiKeyEl = document.getElementById("field-api-key");
  if (apiKeyEl) apiKeyEl.value = (item.custom_fields && (item.custom_fields.api_key || item.custom_fields.session_token)) || "";
  const isLocked = !!(item.fp_config && item.fp_config.node_locked);
  const proxyInput = document.getElementById("field-proxy");
  proxyInput.value = (item.fp_config && item.fp_config.proxy) || "";
  proxyInput.disabled = isLocked;
  onSecretFieldInput(item.secret_key || "");

  document.getElementById("account-modal").style.display = "flex";
  loadAccountEditHistory(accountId);
}

function closeAccountModal() {
  document.getElementById("account-modal").style.display = "none";
  accountEditDynamicValues = null;
  clearAccountEditHistory();
}

function clearAccountEditHistory() {
  document.getElementById("account-edit-history").hidden = true;
  document.getElementById("account-edit-history-list").replaceChildren();
}

async function loadAccountEditHistory(accountId) {
  const section = document.getElementById("account-edit-history");
  const list = document.getElementById("account-edit-history-list");
  const message = document.createElement("p");
  message.textContent = "正在读取变更历史…";
  list.replaceChildren(message);
  section.hidden = false;
  try {
    const result = await callApi("get_account_change_history", accountId);
    if (!message.isConnected) return;
    if (!result?.ok) throw new Error(result?.error || "读取失败");
    if (!result.history.length) {
      message.textContent = "暂无变更记录。";
      return;
    }

    const entries = document.createDocumentFragment();
    for (const entry of result.history) {
      const row = document.createElement("article");
      row.className = "account-edit-history-entry";
      row.innerHTML = `<div class="account-edit-history-meta"><strong></strong><time></time></div>
        <div class="account-edit-history-values"><div>修改前 <span></span></div><div>修改后 <span></span></div></div>`;
      row.querySelector("strong").textContent = entry.label || entry.field;
      const time = row.querySelector("time");
      time.dateTime = entry.created_at;
      time.textContent = new Date(entry.created_at).toLocaleString();
      const [before, after] = row.querySelectorAll(".account-edit-history-values span");
      const format = (value) => {
        if (value == null || value === "") return "（空）";
        if (typeof value === "boolean") return value ? "是" : "否";
        return typeof value === "object" ? JSON.stringify(value) : String(value);
      };
      const renderValues = (reveal) => {
        before.textContent = reveal || !entry.old_value ? format(entry.old_value) : "••••••••";
        after.textContent = reveal || !entry.new_value ? format(entry.new_value) : "••••••••";
      };
      renderValues(!entry.sensitive);
      if (entry.sensitive) {
        const reveal = document.createElement("button");
        reveal.type = "button";
        reveal.className = "btn btn-ghost btn-xs";
        reveal.textContent = "显示原值";
        reveal.setAttribute("aria-pressed", "false");
        reveal.addEventListener("click", () => {
          const visible = reveal.getAttribute("aria-pressed") !== "true";
          reveal.setAttribute("aria-pressed", String(visible));
          reveal.textContent = visible ? "隐藏原值" : "显示原值";
          renderValues(visible);
        });
        row.querySelector(".account-edit-history-meta").append(reveal);
      }
      entries.append(row);
    }
    list.replaceChildren(entries);
  } catch (error) {
    message.textContent = "变更历史读取失败: " + error.message;
  }
}

function setRadioHealth(status) {
  const targetVal = status || "可用";
  const radio = document.querySelector(`input[name="health_status"][value="${targetVal}"]`);
  if (radio) radio.checked = true;
}

function getRadioHealth() {
  const checked = document.querySelector('input[name="health_status"]:checked');
  return checked ? checked.value : "可用";
}

function setRadioAntigravity(tier) {
  let targetVal = tier || "无";
  if (targetVal === "蓝色背景色Pro" || targetVal === "蓝Pro") targetVal = "蓝色Pro";
  if (targetVal === "金色背景色Pro" || targetVal === "金Pro") targetVal = "金色Pro";
  const radio = document.querySelector(`input[name="antigravity_tier"][value="${targetVal}"]`);
  if (radio) radio.checked = true;
}

function getRadioAntigravity() {
  const checked = document.querySelector('input[name="antigravity_tier"]:checked');
  return checked ? checked.value : "无";
}

function setRadioDevice(device) {
  const targetVal = device || "主机";
  const radio = document.querySelector(`input[name="device_tag"][value="${targetVal}"]`);
  if (radio) radio.checked = true;
}

function getRadioDevice() {
  const checked = document.querySelector('input[name="device_tag"]:checked');
  return checked ? checked.value : "主机";
}

function resetFormFields() {
  document.getElementById("field-id").value = "";
  setRadioType("ChatGPT");
  setRadioHealth("可用");
  setRadioAntigravity("无");
  setRadioDevice("主机");
  document.getElementById("field-password-modified").checked = false;
  document.getElementById("field-twofa-modified").checked = false;
  document.getElementById("field-account").value = "";
  const pwdEl = document.getElementById("field-password");
  pwdEl.value = "";
  pwdEl.oninput = null;
  document.getElementById("field-secret").value = "";
  document.getElementById("field-initial-secret").value = "";
  document.getElementById("field-purchase-date").value = "";
  document.getElementById("field-deactivated-date").value = "";
  document.getElementById("field-recovery").value = "";
  document.getElementById("field-year").value = "";
  document.getElementById("field-region").value = "";
  document.getElementById("field-note").value = "";
  const recPwdEl = document.getElementById("field-recovery-password");
  if (recPwdEl) recPwdEl.value = "";
  const apiKeyEl = document.getElementById("field-api-key");
  if (apiKeyEl) apiKeyEl.value = "";
  const personaPrev = document.getElementById("persona-preview-text");
  if (personaPrev) personaPrev.textContent = "";
  const proxyInput = document.getElementById("field-proxy");
  proxyInput.value = "";
  proxyInput.disabled = false;
  document.getElementById("secret-live-preview").textContent = "填写后将自动计算 6 位动态验证码";
}

function setRadioType(type) {
  const radio = document.querySelector(`input[name="account_type"][value="${type}"]`);
  if (radio) radio.checked = true;
}

function getRadioType() {
  const checked = document.querySelector('input[name="account_type"]:checked');
  return checked ? checked.value : "ChatGPT";
}

function resetFeedbackText() {
  const fb = document.getElementById("smart-parse-feedback");
  fb.className = "smart-parse-feedback";
  fb.textContent = "粘贴后将自动识别：账号、密码、初始/新2FA秘钥、购买与停用日期、辅助邮箱、年份、地区与备注";
}

function clearSmartPaste() {
  document.getElementById("smart-paste-input").value = "";
  document.getElementById("batch-banner").style.display = "none";
  state.parsedBatchRecords = [];
  resetFeedbackText();
}

async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      const ta = document.getElementById("smart-paste-input");
      ta.value = text;
      await onSmartPasteInput(text);
    }
  } catch (_) {
    showToast("请直接在识别框内按 Ctrl+V 粘贴");
  }
}

function normalizeSecretString(val) {
  let raw = String(val || "").trim();
  if (/^otpauth:\/\//i.test(raw)) {
    try {
      const u = new URL(raw);
      const sec = u.searchParams.get("secret");
      if (sec) raw = sec;
    } catch (_) {}
  }
  return raw.replace(/[\s\-]+/g, "").toUpperCase();
}

let smartParseRevision = 0;
async function onSmartPasteInput(rawText) {
  const revision = ++smartParseRevision;
  const fb = document.getElementById("smart-parse-feedback");
  const batchBanner = document.getElementById("batch-banner");

  if (!rawText || !rawText.trim()) {
    batchBanner.style.display = "none";
    state.parsedBatchRecords = [];
    resetFeedbackText();
    return;
  }

  try {
    const res = await callApi("smart_parse", rawText, state.importAccountType);
    if (revision !== smartParseRevision) return;
    if (res && res.only_secret) {
      batchBanner.style.display = "none";
      const sObj = res.only_secret;
      const cleaned = normalizeSecretString(sObj.secret_key);
      const secretEl = document.getElementById("field-secret");
      const initEl = document.getElementById("field-initial-secret");
      if (getRadioType() !== "Google" && secretEl.value && secretEl.value !== cleaned && !initEl.value) {
        initEl.value = normalizeSecretString(secretEl.value);
        document.getElementById("field-twofa-modified").checked = true;
      } else if (getRadioType() !== "Google" && sObj.twofa_modified) {
        document.getElementById("field-twofa-modified").checked = true;
      }
      secretEl.value = cleaned;
      document.getElementById("smart-paste-input").value = cleaned;
      if (sObj.totp && sObj.totp.valid) {
        document.getElementById("secret-live-preview").textContent = `✓ 已去空格并转大写 (${sObj.masked_secret}) · 当前动态验证码: ${formatTotpCode(sObj.totp.code)}`;
      }
      fb.className = "smart-parse-feedback recognized";
      fb.textContent = "✓ 已识别为 2FA 秘钥，自动去除空格并转为大写填入下方 2FA 栏！";
      return;
    }

    const records = res.records || [];
    state.parsedBatchRecords = records;

    if (records.length === 0) {
      batchBanner.style.display = "none";
      fb.className = "smart-parse-feedback";
      fb.textContent = res.error || "未识别到标准分隔格式，请在下方手动填写或检查粘贴内容";
      return;
    }

    const first = records[0];
    const recType = first.account_type || state.importAccountType || "ChatGPT";
    syncAccountTypeRadios(recType);
    renderAccountModalDynamicFields(recType, {
      health_status: first.health_status || "可用",
      antigravity_tier: first.antigravity_tier || "无",
      device_tag: first.device_tag || "主机",
    });
    if (recType !== "Google") {
      if (first.password_modified) {
        document.getElementById("field-password-modified").checked = true;
      }
      document.getElementById("field-twofa-modified").checked = !!first.twofa_modified;
    }
    document.getElementById("field-account").value = first.account || "";
    document.getElementById("field-password").value = first.password || "";
    document.getElementById("field-secret").value = normalizeSecretString(first.secret_key);
    document.getElementById("field-initial-secret").value = normalizeSecretString(first.initial_secret_key);
    document.getElementById("field-purchase-date").value = first.purchase_date || "";
    document.getElementById("field-deactivated-date").value = first.deactivated_date || "";
    document.getElementById("field-recovery").value = first.recovery_email || "";
    document.getElementById("field-year").value = first.reg_year || "";
    document.getElementById("field-region").value = first.reg_region || "";
    if (first.note) document.getElementById("field-note").value = first.note;

    if (first.totp && first.totp.valid) {
      document.getElementById("secret-live-preview").textContent = `✓ 2FA秘钥有效 (已去空格转大写) · 当前动态验证码: ${formatTotpCode(first.totp.code)}`;
    } else if (first.secret_key) {
      document.getElementById("secret-live-preview").textContent = "⚠ 2FA秘钥格式非标准Base32，仍可原样保存";
    } else {
      document.getElementById("secret-live-preview").textContent = "未包含2FA秘钥";
    }

    fb.className = "smart-parse-feedback recognized";
    if (records.length === 1) {
      batchBanner.style.display = "none";
      fb.textContent = `✓ 智能识别成功：已自动识别为 [${first.account_type}] 格式（2FA秘钥已自动去空格转大写）！`;
    } else {
      batchBanner.style.display = "flex";
      document.getElementById("batch-count").textContent = records.length;
      document.getElementById("batch-btn-count").textContent = `${records.length} 条`;
      fb.textContent = `✓ 已识别到 ${records.length} 条账号记录（下方预览第 1 条，可点击上方按钮一键全部导入）`;
    }
  } catch (err) {
    console.error("Smart parse error:", err);
  }
}

async function onSecretFieldInput(secretVal) {
  const secretInput = document.getElementById("field-secret");
  const cleaned = normalizeSecretString(secretVal);
  if (secretInput && secretInput.value !== cleaned) {
    secretInput.value = cleaned;
  }
  const hintEl = document.getElementById("secret-live-preview");
  if (!cleaned) {
    hintEl.textContent = "填写后将自动计算 6 位动态验证码";
    return;
  }
  try {
    const res = await callApi("preview_totp", cleaned);
    if (res.totp && res.totp.valid) {
      hintEl.textContent = `✓ 秘钥有效 (${res.masked_secret}) · 当前动态验证码: ${formatTotpCode(res.totp.code)}`;
    } else {
      hintEl.textContent = "⚠ 秘钥非标准 Base32 格式，无法生成动态验证码（仍可保存）";
    }
  } catch (_) {}
}

function onInitialSecretFieldInput(secretVal) {
  const initInput = document.getElementById("field-initial-secret");
  const cleaned = normalizeSecretString(secretVal);
  if (initInput && initInput.value !== cleaned) {
    initInput.value = cleaned;
  }
}

async function submitAccountForm(e) {
  e.preventDefault();
  const accType = getRadioType();
  const dynVals = getCurrentDynamicFormValues();
  const previous = accountEditDynamicValues?.accountType === accType ? accountEditDynamicValues.values : {};
  const custom_fields = {};
  (state.attributes || []).forEach((attr) => {
    const key = attr.attr_key;
    if (!["health_status", "device_tag", "antigravity_tier"].includes(key) &&
        dynVals[key] !== undefined && dynVals[key] !== previous[key]) {
      custom_fields[key] = dynVals[key];
    }
  });
  const recPwdVal = (document.getElementById("field-recovery-password")?.value || "").trim();
  const apiKeyOrSessionVal = (document.getElementById("field-api-key")?.value || "").trim();
  if (recPwdVal || document.getElementById("field-id").value) {
    custom_fields.recovery_password = recPwdVal;
  }
  if (apiKeyOrSessionVal.startsWith("sk-")) {
    custom_fields.api_key = apiKeyOrSessionVal;
    custom_fields.session_token = "";
  } else if (apiKeyOrSessionVal) {
    custom_fields.api_key = "";
    custom_fields.session_token = apiKeyOrSessionVal;
  } else if (document.getElementById("field-id").value) {
    custom_fields.api_key = "";
    custom_fields.session_token = "";
  }

  const payload = {
    id: document.getElementById("field-id").value || null,
    account_type: accType,
    health_status: dynVals.health_status || getRadioHealth(),
    antigravity_tier: dynVals.antigravity_tier || getRadioAntigravity(),
    device_tag: dynVals.device_tag || getRadioDevice(),
    custom_fields,
    password_modified: document.getElementById("field-password-modified").checked ? 1 : 0,
    twofa_modified: document.getElementById("field-twofa-modified").checked ? 1 : 0,
    account: document.getElementById("field-account").value.trim(),
    password: accType === "域名邮箱" ? document.getElementById("field-password").value : document.getElementById("field-password").value.trim(),
    secret_key: normalizeSecretString(document.getElementById("field-secret").value),
    initial_secret_key: normalizeSecretString(document.getElementById("field-initial-secret").value),
    purchase_date: document.getElementById("field-purchase-date").value.trim(),
    deactivated_date: document.getElementById("field-deactivated-date").value.trim(),
    recovery_email: document.getElementById("field-recovery").value.trim(),
    reg_year: document.getElementById("field-year").value.trim(),
    reg_region: document.getElementById("field-region").value.trim(),
    note: document.getElementById("field-note").value.trim(),
    proxy: document.getElementById("field-proxy").value.trim(),
  };

  try {
    const res = await callApi("save_account", payload);
    if (res.ok) {
      closeAccountModal();
      showToast(payload.id ? "✓ 账号更新成功" : "✓ 账号添加成功");
      await loadDashboard();
    } else {
      showToast(res.error || "保存失败");
    }
  } catch (err) {
    showToast("保存失败: " + err.message);
  }
}

async function confirmBatchImport() {
  const rawText = document.getElementById("smart-paste-input").value;
  if (!rawText.trim()) return;
  const strategy = document.getElementById("batch-dup-strategy")?.value || "skip";
  try {
    const res = await callApi("batch_import", rawText, state.importAccountType, strategy);
    if (res.ok) {
      closeAccountModal();
      const parts = [];
      if (res.imported) parts.push(`新增 ${res.imported} 条`);
      if (res.updated) parts.push(`更新 ${res.updated} 条`);
      if (res.skipped) parts.push(`跳过 ${res.skipped} 条`);
      showToast(`✓ 批量导入完成：${parts.join("，") || `导入 ${res.imported} 条`}`);
      await loadDashboard();
    } else {
      showToast(res.error || "批量导入失败");
    }
  } catch (err) {
    showToast("批量导入异常: " + err.message);
  }
}

async function confirmDeleteAccount(accountId) {
  const item = state.accounts.find((a) => a.id === accountId);
  if (!item) return;
  try {
    const res = await callApi("delete_account", accountId);
    if (res.ok) {
      showToast(`已删除账号 ${item.masked_account}`);
      await loadDashboard();
    }
  } catch (err) {
    showToast("删除失败");
  }
}

// Export Modal
async function openExportModal() {
  try {
    const titleEl = document.getElementById("export-modal-title");
    const subEl = document.getElementById("export-modal-sub");
    const dlBtn = document.getElementById("btn-download-export-json");
    if (titleEl) titleEl.textContent = "导出账号备份";
    if (subEl) subEl.textContent = "已按原标准 `----` 分隔格式还原，可直接复制备份";
    if (dlBtn) dlBtn.style.display = "none";
    const res = await callApi("export_text", state.filterType);
    document.getElementById("export-textarea").value = res.text || "";
    document.getElementById("export-modal").style.display = "flex";
  } catch (err) {
    showToast("导出失败");
  }
}

function closeExportModal() {
  document.getElementById("export-modal").style.display = "none";
}

async function copyExportText() {
  const text = document.getElementById("export-textarea").value;
  if (!text) {
    showToast("暂无可导出的账号数据");
    return;
  }
  await writeClipboard(text);
  showToast("✓ 已复制全部导出内容至剪贴板");
}

function showToast(msg) {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => {
    toast.remove();
  }, 2200);
}

// ============================================================
// SYSTEM SETTINGS & DICTIONARY MAINTENANCE CONTROLLERS
// ============================================================
function openSettingsModal() {
  const modal = document.getElementById("settings-modal");
  if (!modal) return;
  modal.style.display = "flex";
  switchSettingsTab("general");

  const typeDicts = (state.dicts || []).filter((d) => d.dict_type === "account_type");
  const devDicts = (state.dicts || []).filter((d) => d.dict_type === "device_tag");
  const selPlat = document.getElementById("set-default-account-type");
  if (selPlat) {
    selPlat.innerHTML = typeDicts
      .map((d) => `<option value="${escapeHtml(d.dict_key)}">${escapeHtml(d.dict_label || d.dict_key)}</option>`)
      .join("");
  }
  const selDev = document.getElementById("set-default-device-tag");
  if (selDev) {
    selDev.innerHTML = devDicts
      .map((d) => `<option value="${escapeHtml(d.dict_key)}">${escapeHtml(d.dict_label || d.dict_key)}</option>`)
      .join("");
  }

  // Load saved general settings into fields
  if (state.settings) {
    const defView = document.getElementById("set-default-view");
    if (defView && state.settings.view_mode) defView.value = state.settings.view_mode;
    const defTotp = document.getElementById("set-totp-interval");
    if (defTotp && state.settings.totp_interval) defTotp.value = String(state.settings.totp_interval);
    if (selPlat && state.settings.default_account_type) selPlat.value = state.settings.default_account_type;
    if (selDev && state.settings.default_device_tag) selDev.value = state.settings.default_device_tag;

    const prefSel = document.getElementById("set-preferred-browser");
    if (prefSel) prefSel.value = state.settings.preferred_browser || "auto";
    const customPathEl = document.getElementById("set-custom-browser-path");
    if (customPathEl) customPathEl.value = state.settings.custom_browser_path || "";
    const langSel = document.getElementById("set-browser-lang");
    if (langSel) langSel.value = state.settings.default_browser_lang || "match_region";
    const startUrlSel = document.getElementById("set-default-start-url");
    if (startUrlSel) startUrlSel.value = state.settings.default_start_url || "https://myaccount.google.com/";
    const geoChk = document.getElementById("set-auto-probe-geo");
    if (geoChk) geoChk.checked = state.settings.auto_probe_geo_on_launch !== false;
    const urlChk = document.getElementById("set-url-interception");
    if (urlChk) {
      urlChk.checked = !!(state.settings.url_interception_live ?? state.settings.url_interception_enabled);
    }
  }

  // Sync custom selects
  syncCustomSelect(document.getElementById("set-default-view"));
  syncCustomSelect(document.getElementById("set-totp-interval"));
  syncCustomSelect(selPlat);
  syncCustomSelect(selDev);
  syncCustomSelect(document.getElementById("set-preferred-browser"));
  syncCustomSelect(document.getElementById("set-browser-lang"));
  syncCustomSelect(document.getElementById("set-default-start-url"));
  onPreferredBrowserChange();

  const offsetEl = document.getElementById("lbl-totp-offset");
  if (offsetEl) {
    const off = Number(state.settings?.totp_offset_seconds || 0);
    offsetEl.textContent = `${off > 0 ? "+" : ""}${off} 秒`;
  }
  loadBackupsList();
}

function closeSettingsModal() {
  const modal = document.getElementById("settings-modal");
  if (modal) modal.style.display = "none";
}

function switchSettingsTab(tabName) {
  state.currentDictTab = tabName;
  document.querySelectorAll(".settings-tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("data-tab") === tabName);
  });

  const paneGeneral = document.getElementById("pane-general");
  const paneDicts = document.getElementById("pane-dicts");
  const paneAttrs = document.getElementById("pane-attrs");

  if (tabName === "general") {
    if (paneGeneral) paneGeneral.style.display = "block";
    if (paneDicts) paneDicts.style.display = "none";
    if (paneAttrs) paneAttrs.style.display = "none";
  } else if (tabName === "attrs") {
    if (paneGeneral) paneGeneral.style.display = "none";
    if (paneDicts) paneDicts.style.display = "none";
    if (paneAttrs) paneAttrs.style.display = "block";
    renderAttributesPane();
  } else {
    if (paneGeneral) paneGeneral.style.display = "none";
    if (paneDicts) paneDicts.style.display = "block";
    if (paneAttrs) paneAttrs.style.display = "none";
    const dictType = tabName.replace("dict-", "");
    renderDictPane(dictType);
  }
}

function renderDictPane(dictType) {
  const titles = {
    account_type: "🤖 账号类型字典表",
    device_tag: "📱 设备标记字典表",
    health_status: "🟢 可用状态字典表",
    antigravity_tier: "💎 Pro 选项字典表",
    wb_reg_status: "💼 WorkBuddy 注册状态字典表",
  };
  const subs = {
    account_type: "配置支持的账号平台（如 ChatGPT, Google, WorkBuddy, Qcoder 等）",
    device_tag: "配置设备标签（如 主机, iPhone, Pad, MacBook 等）",
    health_status: "配置账号可用性状态（如 可用, 封号, 人机, 短信, 验证 等）",
    antigravity_tier: "配置 Antigravity 选项（无, 蓝色Pro, 金色Pro 等）",
  };

  const titleEl = document.getElementById("dict-pane-title");
  const subEl = document.getElementById("dict-pane-sub");
  if (titleEl) titleEl.textContent = titles[dictType] || "字典表维护";
  if (subEl) subEl.textContent = subs[dictType] || "支持添加与安全维护";

  const tbody = document.getElementById("dict-table-body");
  if (!tbody) return;

  const items = (state.dicts || []).filter((d) => d.dict_type === dictType);
  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:20px;">暂无该类型字典数据</td></tr>`;
    return;
  }

  tbody.innerHTML = items
    .map(
      (item) => `
    <tr>
      <td><strong>${escapeHtml(item.dict_key)}</strong></td>
      <td>
        <span class="dict-badge">
          ${item.icon ? `<span>${escapeHtml(item.icon)}</span>` : ""}
          <span>${escapeHtml(item.dict_label || item.dict_key)}</span>
        </span>
      </td>
      <td>${item.sort_order}</td>
      <td>${item.is_default ? '<span style="color:var(--color-chatgpt);font-weight:700;">✓ 默认</span>' : '-'}</td>
      <td>
        <div class="dict-ops">
          <button class="tool-link" onclick="openEditDictModal(${item.id})">编辑</button>
          ${
            item.is_system
              ? '<span style="color:var(--text-muted);font-size:11px;" title="内置项不可删除">内置保护</span>'
              : `<button class="tool-link danger" onclick="confirmDeleteDict(${item.id})">删除</button>`
          }
        </div>
      </td>
    </tr>
  `
    )
    .join("");
}

function openAddDictModal() {
  state.editingDictId = null;
  const modal = document.getElementById("dict-edit-modal");
  const form = document.getElementById("dict-edit-form");
  const title = document.getElementById("dict-modal-title");
  if (!modal || !form) return;

  form.reset();
  const dictType = state.currentDictTab.replace("dict-", "");
  document.getElementById("dict-field-id").value = "";
  document.getElementById("dict-field-type").value = dictType;
  if (title) title.textContent = "添加字典项";
  modal.style.display = "flex";
}

function openEditDictModal(dictId) {
  state.editingDictId = dictId;
  const item = (state.dicts || []).find((d) => d.id === dictId);
  if (!item) return;

  const modal = document.getElementById("dict-edit-modal");
  const title = document.getElementById("dict-modal-title");
  if (!modal) return;

  document.getElementById("dict-field-id").value = item.id;
  document.getElementById("dict-field-type").value = item.dict_type;
  document.getElementById("dict-field-key").value = item.dict_key;
  document.getElementById("dict-field-key").readOnly = !!item.is_system;
  document.getElementById("dict-field-label").value = item.dict_label || item.dict_key;
  document.getElementById("dict-field-sort").value = item.sort_order || 0;
  document.getElementById("dict-field-default").checked = !!item.is_default;

  if (title) title.textContent = `编辑「${item.dict_label || item.dict_key}」`;
  modal.style.display = "flex";
}

function closeDictEditModal() {
  const modal = document.getElementById("dict-edit-modal");
  if (modal) modal.style.display = "none";
}

async function submitDictForm(event) {
  event.preventDefault();
  const idVal = document.getElementById("dict-field-id").value;
  const dict_type = document.getElementById("dict-field-type").value;
  const dict_key = document.getElementById("dict-field-key").value.trim();
  const dict_label = document.getElementById("dict-field-label").value.trim();
  const sort_order = parseInt(document.getElementById("dict-field-sort").value, 10) || 0;
  const is_default = document.getElementById("dict-field-default").checked ? 1 : 0;

  const payload = {
    dict_type,
    dict_key,
    dict_label,
    dict_value: dict_key,
    sort_order,
    is_default,
  };
  if (idVal) payload.id = parseInt(idVal, 10);

  try {
    const res = await callApi("save_system_dict", payload);
    if (res && res.ok) {
      showToast("✓ 字典项已保存");
      closeDictEditModal();
      await loadDashboard();
      renderDictPane(dict_type);
    } else {
      showToast(res.error || "保存字典项失败");
    }
  } catch (err) {
    showToast("保存异常: " + err.message);
  }
}

async function confirmDeleteDict(dictId) {
  const item = (state.dicts || []).find((d) => d.id === dictId);
  const name = item ? item.dict_label || item.dict_key : "该字典项";
  if (!confirm(`确定要删除字典项「${name}」吗？`)) return;

  try {
    const res = await callApi("delete_system_dict", dictId);
    if (res && res.ok) {
      showToast("✓ 字典项已删除");
      await loadDashboard();
      if (item) renderDictPane(item.dict_type);
    } else {
      showToast(res.error || "删除失败");
    }
  } catch (err) {
    showToast("删除异常: " + err.message);
  }
}

function onPreferredBrowserChange() {
  const pref = document.getElementById("set-preferred-browser")?.value || "auto";
  const grp = document.getElementById("set-custom-browser-group");
  if (grp) grp.style.display = pref === "custom" ? "block" : "none";
}

async function onClickPickBrowserExe() {
  try {
    const res = await callApi("pick_browser_executable");
    if (res && res.ok && !res.canceled && res.path) {
      const inp = document.getElementById("set-custom-browser-path");
      if (inp) inp.value = res.path;
      const prefSel = document.getElementById("set-preferred-browser");
      if (prefSel) {
        prefSel.value = "custom";
        syncCustomSelect(prefSel);
      }
      onPreferredBrowserChange();
    }
  } catch (err) {
    showToast("选择浏览器路径失败: " + err.message);
  }
}

async function saveGeneralSettings() {
  const viewMode = document.getElementById("set-default-view")?.value || "compact";
  const totpInterval = parseInt(document.getElementById("set-totp-interval")?.value, 10) || 30;
  const defPlat = document.getElementById("set-default-account-type")?.value || "ChatGPT";
  const defDev = document.getElementById("set-default-device-tag")?.value || "主机";
  const preferredBrowser = document.getElementById("set-preferred-browser")?.value || "auto";
  const customBrowserPath = (document.getElementById("set-custom-browser-path")?.value || "").trim();
  const defaultBrowserLang = document.getElementById("set-browser-lang")?.value || "match_region";
  const defaultStartUrl = document.getElementById("set-default-start-url")?.value || "https://myaccount.google.com/";
  const autoProbeGeo = !!document.getElementById("set-auto-probe-geo")?.checked;
  const urlIntercept = !!document.getElementById("set-url-interception")?.checked;

  const urlChk = document.getElementById("set-url-interception");
  const prevIntercept = !!(state.settings?.url_interception_live ?? state.settings?.url_interception_enabled);
  try {
    const updated = await callApi("save_settings", {
      view_mode: viewMode,
      totp_interval: totpInterval,
      default_account_type: defPlat,
      default_device_tag: defDev,
      preferred_browser: preferredBrowser,
      custom_browser_path: customBrowserPath,
      default_browser_lang: defaultBrowserLang,
      default_start_url: defaultStartUrl,
      auto_probe_geo_on_launch: autoProbeGeo,
    });
    state.settings = updated;
    if (urlIntercept !== prevIntercept) {
      const tRes = await callApi("toggle_url_interception", urlIntercept);
      if (!tRes || !tRes.ok) {
        if (urlChk) urlChk.checked = !!(tRes && tRes.enabled);
        state.settings.url_interception_live = !!(tRes && tRes.enabled);
        showToast((tRes && tRes.error) || "外链接管切换失败，其余设置已保存");
        applyViewMode(viewMode, false);
        await loadDashboard();
        return;
      }
      state.settings.url_interception_live = !!tRes.enabled;
    }
    showToast("✓ 系统常用设置与浏览器防风控配置已保存");
    applyViewMode(viewMode, false);
    closeSettingsModal();
    await loadDashboard();
  } catch (err) {
    showToast("保存设置失败: " + err.message);
  }
}

// ============================================================

// ============================================================
// UNIVERSAL CUSTOM SELECT DROPDOWN SYSTEM
// ============================================================
function enhanceSelectToCustom(selectEl) {
  if (!selectEl) return;
  if (selectEl._customContainer) {
    syncCustomSelect(selectEl);
    return;
  }

  // Create wrapper container with isolated component classes
  const wrap = document.createElement("div");
  wrap.className = "c-select-container";
  if (selectEl.id) wrap.classList.add("c-select-" + selectEl.id);
  if (selectEl.classList.contains("topbar-select")) wrap.classList.add("c-topbar-select");
  if (selectEl.classList.contains("health-filter-select")) wrap.classList.add("c-filter-select");
  if (selectEl.classList.contains("settings-select")) wrap.classList.add("c-settings-select");
  if (selectEl.classList.contains("port-select")) wrap.classList.add("c-port-select");
  if (selectEl.classList.contains("ag-filter-select")) wrap.classList.add("c-ag-select");
  wrap.setAttribute("data-for-id", selectEl.id || "");

  // Create trigger button
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "c-select-trigger";
  trigger.innerHTML = `
    <span class="c-select-label"></span>
    <svg class="c-select-arrow" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="6 9 12 15 18 9"></polyline>
    </svg>
  `;

  // Create dropdown menu
  const menu = document.createElement("div");
  menu.className = "c-select-menu";

  wrap.appendChild(trigger);
  wrap.appendChild(menu);

  // Insert before the select
  selectEl.parentNode.insertBefore(wrap, selectEl);
  selectEl.style.display = "none";
  selectEl._customContainer = wrap;

  // Toggle dropdown on trigger click
  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    if (selectEl.disabled || wrap.classList.contains("is-disabled")) {
      return;
    }
    const isOpen = wrap.classList.contains("open");
    closeAllCustomSelects();
    closeAllHealthPopovers();
    if (!isOpen) {
      wrap.classList.add("open");
      // Adjust positioning if menu exceeds viewport bottom
      const rect = wrap.getBoundingClientRect();
      const menuHeight = menu.offsetHeight || 220;
      if (rect.bottom + menuHeight > window.innerHeight && rect.top > menuHeight) {
        wrap.classList.add("drop-up");
      } else {
        wrap.classList.remove("drop-up");
      }
    }
  });

  // Sync content initially
  syncCustomSelect(selectEl);
}

function syncCustomSelect(selectEl) {
  if (!selectEl) return;
  if (!selectEl._customContainer) {
    enhanceSelectToCustom(selectEl);
    return;
  }
  const wrap = selectEl._customContainer;
  const trigger = wrap.querySelector(".c-select-trigger");
  const labelEl = trigger.querySelector(".c-select-label");
  const menu = wrap.querySelector(".c-select-menu");
  const isDisabled = !!selectEl.disabled;
  trigger.disabled = isDisabled;
  wrap.classList.toggle("is-disabled", isDisabled);
  if (isDisabled) wrap.classList.remove("open", "drop-up");

  const selectedOpt = selectEl.options[selectEl.selectedIndex] || selectEl.options[0];
  const selectedVal = selectEl.value;

  // Update label
  if (selectedOpt) {
    labelEl.textContent = selectedOpt.textContent;
  } else {
    labelEl.textContent = "";
  }

  // Populate options
  menu.innerHTML = "";
  Array.from(selectEl.options).forEach((opt) => {
    const item = document.createElement("div");
    item.className = "c-select-option" + (opt.value === selectedVal ? " active" : "");
    item.setAttribute("data-value", opt.value);
    item.innerHTML = `
      <span class="c-option-text">${escapeHtml(opt.textContent)}</span>
      ${opt.value === selectedVal ? '<span class="c-option-check">✓</span>' : ''}
    `;

    item.addEventListener("click", (e) => {
      e.stopPropagation();
      if (selectEl.disabled) return;
      selectEl.value = opt.value;
      wrap.classList.remove("open");
      wrap.classList.remove("drop-up");
      syncCustomSelect(selectEl);
      selectEl.dispatchEvent(new Event("change", { bubbles: true }));
    });

    menu.appendChild(item);
  });
}

function closeAllCustomSelects() {
  document.querySelectorAll(".c-select-container.open").forEach((wrap) => {
    wrap.classList.remove("open");
    wrap.classList.remove("drop-up");
  });
}

function initAllCustomSelects() {
  document.querySelectorAll("select:not([data-native-select])").forEach((sel) => {
    enhanceSelectToCustom(sel);
  });

  // Clicking on stat-pill-platform-wrap should trigger the custom select
  const pillWrap = document.getElementById("stat-pill-platform-wrap");
  if (pillWrap && !pillWrap._hasCustomClickListener) {
    pillWrap._hasCustomClickListener = true;
    pillWrap.addEventListener("click", (e) => {
      if (e.target.closest(".c-select-menu")) return;
      const topSel = document.getElementById("topbar-platform-select");
      if (topSel && topSel._customContainer) {
        const trigger = topSel._customContainer.querySelector(".c-select-trigger");
        if (trigger && !e.target.closest(".c-select-trigger")) {
          trigger.click();
        }
      }
    });
  }
}

// Global click outside listener
document.addEventListener("click", (e) => {
  if (!e.target.closest(".c-select-container")) {
    closeAllCustomSelects();
  }
});

// GLOBAL KEYBOARD LISTENER (ESC to close any open modal)
// ============================================================
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" || e.keyCode === 27) {
    closeAllCustomSelects();
    const quickCredModal = document.getElementById("quick-cred-modal");
    if (quickCredModal && quickCredModal.style.display !== "none") {
      closeQuickCredModal();
      return;
    }
    const rtOAuthModal = document.getElementById("rt-oauth-modal");
    if (rtOAuthModal && rtOAuthModal.style.display !== "none") {
      closeRtOAuthModal();
      return;
    }
    const appealModal = document.getElementById("appeal-modal");
    if (appealModal && appealModal.style.display !== "none") {
      closeAppealModal();
      return;
    }
    const attrEditModal = document.getElementById("attr-edit-modal");
    if (attrEditModal && attrEditModal.style.display !== "none") {
      if (typeof closeAttrEditModal === "function") closeAttrEditModal();
      return;
    }
    const dictEditModal = document.getElementById("dict-edit-modal");
    if (dictEditModal && dictEditModal.style.display !== "none") {
      closeDictEditModal();
      return;
    }
    const settingsModal = document.getElementById("settings-modal");
    if (settingsModal && settingsModal.style.display !== "none") {
      closeSettingsModal();
      return;
    }
    const nodeModal = document.getElementById("node-modal");
    if (nodeModal && nodeModal.style.display !== "none") {
      closeNodeModal();
      return;
    }
    const exportModal = document.getElementById("export-modal");
    if (exportModal && exportModal.style.display !== "none") {
      if (typeof closeExportModal === "function") closeExportModal();
      else exportModal.style.display = "none";
      return;
    }
    const accountModal = document.getElementById("account-modal");
    if (accountModal && accountModal.style.display !== "none") {
      closeAccountModal();
      return;
    }
  }
});


// ============================================================
// ATTRIBUTE & PLATFORM BINDING CONTROLLER
// ============================================================
function renderAttributesPane() {
  const tbody = document.getElementById("attrs-table-body");
  if (!tbody) return;
  if (!tbody._delegated) {
    tbody._delegated = true;
    tbody.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-dict-tab]");
      if (btn) {
        switchSettingsTab("dict-" + (btn.getAttribute("data-dict-tab") || ""));
      }
    });
  }

  const items = state.attributes || [];
  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:20px;">暂无属性定义数据</td></tr>`;
    return;
  }

  tbody.innerHTML = items
    .map((item) => {
      const isSys = !!item.is_system;
      const typeBadge =
        item.attr_type === "select"
          ? `<span class="attr-type-badge type-select">下拉单选</span>${
              item.dict_type ? `<span style="font-size:11px;color:var(--text-muted);margin-left:4px;">(${escapeHtml(item.dict_type)})</span>` : ""
            }`
          : `<span class="attr-type-badge type-text">单行文本</span>`;

      let platformBadges = "";
      if (item.bound_platforms === "All") {
        platformBadges = `<span class="attr-platform-tag tag-all">全部平台</span>`;
      } else {
        const list = Array.isArray(item.platforms_list)
          ? item.platforms_list
          : (() => {
              try {
                return JSON.parse(item.bound_platforms || "[]");
              } catch (e) {
                return [];
              }
            })();
        if (list.length === 0) {
          platformBadges = `<span class="attr-platform-tag tag-all">全部平台</span>`;
        } else {
          platformBadges = list
            .map((p) => `<span class="attr-platform-tag">${escapeHtml(p)}</span>`)
            .join("");
        }
      }

      return `
      <tr>
        <td>
          <strong>${escapeHtml(item.attr_label)}</strong>
          ${isSys ? '<span class="attr-system-pill">内置</span>' : ""}
        </td>
        <td><code>${escapeHtml(item.attr_key)}</code></td>
        <td>${typeBadge}</td>
        <td><div style="display:flex;flex-wrap:wrap;gap:4px;">${platformBadges}</div></td>
        <td>${item.sort_order || 0}</td>
        <td>
          <div class="dict-ops">
            <button class="tool-link" onclick="openEditAttrModal(${item.id})">编辑</button>
            ${
              item.attr_type === "select" && item.dict_type
                ? `<button class="tool-link" style="color:var(--color-chatgpt);" data-dict-tab="${escapeHtml(
                    item.dict_type
                  )}">维护字典</button>`
                : ""
            }
            ${
              isSys
                ? '<span style="color:var(--text-muted);font-size:11px;" title="系统内置属性不可删除">内置保护</span>'
                : `<button class="tool-link danger" onclick="confirmDeleteAttr(${item.id})">删除</button>`
            }
          </div>
        </td>
      </tr>
      `;
    })
    .join("");
}

function renderAttrPlatformCheckboxes(boundPlatforms) {
  const container = document.getElementById("attr-platform-checkboxes");
  if (!container) return;

  const typeDicts = (state.dicts || []).filter((d) => d.dict_type === "account_type");
  const platformOptions = [
    { key: "All", label: "全部平台", icon: "🌐" },
    ...typeDicts.map((d) => ({
      key: d.dict_key,
      label: d.dict_label || d.dict_key,
      icon: d.icon || "💻",
    })),
  ];

  let selectedSet = new Set();
  if (boundPlatforms === "All" || !boundPlatforms) {
    selectedSet.add("All");
  } else {
    try {
      const arr = typeof boundPlatforms === "string" ? JSON.parse(boundPlatforms) : boundPlatforms;
      if (Array.isArray(arr)) {
        if (arr.length === 0 || arr.includes("All")) {
          selectedSet.add("All");
        } else {
          arr.forEach((p) => selectedSet.add(p));
        }
      } else {
        selectedSet.add("All");
      }
    } catch (e) {
      selectedSet.add("All");
    }
  }

  container.innerHTML = platformOptions
    .map((opt) => {
      const checked = selectedSet.has(opt.key);
      return `
      <label class="platform-check-item ${checked ? "active" : ""}">
        <input type="checkbox" name="attr_platform" value="${escapeHtml(opt.key)}" ${
        checked ? "checked" : ""
      } onchange="onAttrPlatformCheckboxChange(this)" style="display:none;" />
        <span>${opt.icon ? `${escapeHtml(opt.icon)} ` : ""}${escapeHtml(opt.label)}</span>
      </label>
      `;
    })
    .join("");
}

function onAttrPlatformCheckboxChange(cb) {
  const val = cb.value;
  const isChecked = cb.checked;
  const container = document.getElementById("attr-platform-checkboxes");
  if (!container) return;

  const allInputs = container.querySelectorAll("input[name='attr_platform']");
  if (val === "All" && isChecked) {
    allInputs.forEach((inp) => {
      if (inp.value !== "All") inp.checked = false;
    });
  } else if (val !== "All" && isChecked) {
    allInputs.forEach((inp) => {
      if (inp.value === "All") inp.checked = false;
    });
  }

  const anyChecked = Array.from(allInputs).some((inp) => inp.checked);
  if (!anyChecked) {
    allInputs.forEach((inp) => {
      if (inp.value === "All") inp.checked = true;
    });
  }

  allInputs.forEach((inp) => {
    const parent = inp.closest(".platform-check-item");
    if (parent) parent.classList.toggle("active", inp.checked);
  });
}

function onAttrTypeChange() {
  const typeSel = document.getElementById("attr-field-type");
  const dictGrp = document.getElementById("attr-dict-type-group");
  if (typeSel && dictGrp) {
    dictGrp.style.display = typeSel.value === "select" ? "block" : "none";
  }
}

function populateDictTypeOptions(selectedType = "") {
  const sel = document.getElementById("attr-field-dict-type");
  if (!sel) return;

  const dictTypes = Array.from(new Set((state.dicts || []).map((d) => d.dict_type)));
  if (!dictTypes.includes("account_type")) dictTypes.push("account_type");
  if (!dictTypes.includes("device_tag")) dictTypes.push("device_tag");
  if (!dictTypes.includes("health_status")) dictTypes.push("health_status");
  if (!dictTypes.includes("antigravity_tier")) dictTypes.push("antigravity_tier");

  const defOpt = `<option value="">自动以标识键作为字典分类 (推荐)</option>`;
  sel.innerHTML =
    defOpt +
    dictTypes
      .map(
        (dt) =>
          `<option value="${escapeHtml(dt)}" ${dt === selectedType ? "selected" : ""}>${escapeHtml(dt)}</option>`
      )
      .join("");
  syncCustomSelect(sel);
}

function openAddAttrModal() {
  state.editingAttrId = null;
  const modal = document.getElementById("attr-edit-modal");
  const form = document.getElementById("attr-edit-form");
  const title = document.getElementById("attr-modal-title");
  const help = document.getElementById("attr-key-help");
  const keyInput = document.getElementById("attr-field-key");
  if (!modal || !form) return;

  form.reset();
  document.getElementById("attr-field-id").value = "";
  if (keyInput) {
    keyInput.readOnly = false;
    keyInput.value = "";
  }
  if (help) help.style.display = "none";
  if (title) title.textContent = "新建属性与平台绑定";

  const typeSel = document.getElementById("attr-field-type");
  if (typeSel) {
    typeSel.value = "text";
    syncCustomSelect(typeSel);
  }
  populateDictTypeOptions();
  onAttrTypeChange();
  renderAttrPlatformCheckboxes("All");

  modal.style.display = "flex";
}

function openEditAttrModal(attrId) {
  state.editingAttrId = attrId;
  const item = (state.attributes || []).find((a) => a.id === attrId);
  if (!item) return;

  const modal = document.getElementById("attr-edit-modal");
  const title = document.getElementById("attr-modal-title");
  const help = document.getElementById("attr-key-help");
  const keyInput = document.getElementById("attr-field-key");
  if (!modal) return;

  document.getElementById("attr-field-id").value = item.id;
  document.getElementById("attr-field-label").value = item.attr_label || "";
  if (keyInput) {
    keyInput.value = item.attr_key || "";
    keyInput.readOnly = !!item.is_system;
  }
  if (help) {
    help.style.display = item.is_system ? "block" : "none";
  }

  const typeSel = document.getElementById("attr-field-type");
  if (typeSel) {
    typeSel.value = item.attr_type || "text";
    syncCustomSelect(typeSel);
  }

  populateDictTypeOptions(item.dict_type || "");
  onAttrTypeChange();

  document.getElementById("attr-field-sort").value = item.sort_order || 0;
  document.getElementById("attr-field-filter").checked = item.is_filter !== 0;

  renderAttrPlatformCheckboxes(item.bound_platforms);

  if (title) title.textContent = `编辑「${item.attr_label}」与平台绑定`;
  modal.style.display = "flex";
}

function closeAttrEditModal() {
  const modal = document.getElementById("attr-edit-modal");
  if (modal) modal.style.display = "none";
}

async function submitAttrForm(event) {
  event.preventDefault();
  const idVal = document.getElementById("attr-field-id").value;
  const attr_label = document.getElementById("attr-field-label").value.trim();
  const attr_key = document.getElementById("attr-field-key").value.trim();
  const attr_type = document.getElementById("attr-field-type").value;
  const dict_type = attr_type === "select" ? (document.getElementById("attr-field-dict-type")?.value || "").trim() : "";
  const sort_order = parseInt(document.getElementById("attr-field-sort").value, 10) || 0;
  const is_filter = document.getElementById("attr-field-filter").checked ? 1 : 0;

  const checkedInputs = Array.from(
    document.querySelectorAll("#attr-platform-checkboxes input[name='attr_platform']:checked")
  );
  let bound_platforms = "All";
  if (checkedInputs.some((inp) => inp.value === "All")) {
    bound_platforms = "All";
  } else {
    const list = checkedInputs.map((inp) => inp.value);
    bound_platforms = list.length > 0 ? list : "All";
  }

  const payload = {
    attr_label,
    attr_key,
    attr_type,
    dict_type,
    sort_order,
    is_filter,
    bound_platforms,
  };
  if (idVal) {
    payload.id = parseInt(idVal, 10);
  }

  try {
    const res = await callApi("save_attribute", payload);
    if (res && res.ok) {
      showToast("✓ 属性配置已保存");
      closeAttrEditModal();
      await loadDashboard();
      renderAttributesPane();
    } else {
      showToast(res.error || "保存属性失败");
    }
  } catch (err) {
    showToast("保存异常: " + err.message);
  }
}

async function confirmDeleteAttr(attrId) {
  const item = (state.attributes || []).find((a) => a.id === attrId);
  const name = item ? item.attr_label : "该属性";
  if (!confirm(`确定要删除属性「${name}」吗？`)) return;

  try {
    const res = await callApi("delete_attribute", attrId);
    if (res && res.ok) {
      showToast("✓ 属性已删除");
      await loadDashboard();
      renderAttributesPane();
    } else {
      showToast(res.error || "删除失败");
    }
  } catch (err) {
    showToast("删除异常: " + err.message);
  }
}


// ============================================================
// DYNAMIC ACCOUNT MODAL & DRAWER ATTRIBUTES
// ============================================================
function syncAccountTypeRadios(selectedType = "ChatGPT") {
  const container = document.getElementById("field-type-selector");
  if (!container) return;

  const typeDicts = (state.dicts || []).filter((d) => d.dict_type === "account_type");
  const types =
    typeDicts.length > 0
      ? typeDicts
      : [
          { dict_key: "ChatGPT", dict_label: "ChatGPT", color_class: "chatgpt", icon: "🤖" },
          { dict_key: "Google", dict_label: "Google", color_class: "google", icon: "🔍" },
          { dict_key: "Claude", dict_label: "Claude", color_class: "claude", icon: "✴️" },
          { dict_key: "WorkBuddy", dict_label: "WorkBuddy", color_class: "workbuddy", icon: "💼" },
          { dict_key: "域名邮箱", dict_label: "域名邮箱", color_class: "domain-email", icon: "✉️" },
          { dict_key: "Qcoder", dict_label: "Qcoder", color_class: "qcoder", icon: "⚡" },
        ];

  container.innerHTML = types
    .map((d) => {
      const isChecked = d.dict_key === selectedType;
      const dot = d.color_class
        ? `<span class="badge-dot dot-${escapeHtml(d.color_class)}"></span>`
        : d.icon
        ? `${escapeHtml(d.icon)} `
        : "";
      return `
      <label class="type-radio">
        <input type="radio" name="account_type" value="${escapeHtml(d.dict_key)}" ${
        isChecked ? "checked" : ""
      } onchange="onAccountTypeRadioChange(this.value)" />
        <span class="radio-pill">${dot}${escapeHtml(d.dict_label || d.dict_key)}</span>
      </label>
      `;
    })
    .join("");

  syncDomainEmailLoginHint(selectedType);
}

function renderAccountModalDynamicFields(accountType, currentValues = {}) {
  const container = document.getElementById("form-dynamic-attributes-container");
  if (!container) return;

  const applicableAttrs = (state.attributes || []).filter((a) =>
    isAttributeApplicable(a, accountType)
  );

  if (applicableAttrs.length === 0) {
    container.innerHTML = "";
    return;
  }

  container.innerHTML = applicableAttrs
    .map((attr) => {
      if (attr.attr_key === "health_status") {
        const dictItems = (state.dicts || []).filter((d) => d.dict_type === "health_status");
        const curVal = currentValues.health_status || "可用";
        const radios = (dictItems.length > 0
          ? dictItems
          : [
              { dict_key: "可用", dict_label: "可用", color_class: "available" },
              { dict_key: "封号", dict_label: "封号", color_class: "banned" },
              { dict_key: "人机", dict_label: "人机 (盾)", color_class: "captcha" },
              { dict_key: "短信", dict_label: "短信验证", color_class: "sms" },
              { dict_key: "验证", dict_label: "邮箱/重置验证", color_class: "verify" },
              { dict_key: "不可用", dict_label: "不可用 (闲置)", color_class: "unavailable" },
            ]
        )
          .map((d) => {
            const checked = d.dict_key === curVal ? "checked" : "";
            const cls = d.color_class ? `pill-${escapeHtml(d.color_class)}` : "";
            const dot = d.color_class ? `<span class="badge-dot dot-${escapeHtml(d.color_class)}"></span>` : "";
            return `
            <label class="type-radio">
              <input type="radio" name="health_status" value="${escapeHtml(d.dict_key)}" ${checked} />
              <span class="radio-pill ${cls}">${dot}${escapeHtml(d.dict_label || d.dict_key)}</span>
            </label>
          `;
          })
          .join("");

        return `
        <div class="form-group" style="margin-bottom: 14px;">
          <label>账号可用状态 (维护入口)</label>
          <div class="health-selector" id="field-health-selector">${radios}</div>
        </div>
        `;
      }

      if (attr.attr_key === "device_tag") {
        const dictItems = (state.dicts || []).filter((d) => d.dict_type === "device_tag");
        const curVal = currentValues.device_tag || "主机";
        const radios = (dictItems.length > 0
          ? dictItems
          : [
              { dict_key: "主机", dict_label: "主机", icon: "🖥️" },
              { dict_key: "iPhone", dict_label: "iPhone", icon: "📱" },
              { dict_key: "Pad", dict_label: "Pad", icon: "📱" },
            ]
        )
          .map((d) => {
            const checked = d.dict_key === curVal ? "checked" : "";
            return `
            <label class="type-radio">
              <input type="radio" name="device_tag" value="${escapeHtml(d.dict_key)}" ${checked} />
              <span class="radio-pill"><span class="device-dot"></span>${d.icon ? `${escapeHtml(d.icon)} ` : ""}${escapeHtml(d.dict_label || d.dict_key)}</span>
            </label>
          `;
          })
          .join("");

        return `
        <div class="form-group" style="margin-bottom: 14px;">
          <label>设备标记</label>
          <div class="health-selector" id="field-device-selector">${radios}</div>
        </div>
        `;
      }

      if (attr.attr_key === "antigravity_tier") {
        const dictItems = (state.dicts || []).filter((d) => d.dict_type === "antigravity_tier");
        const curVal = currentValues.antigravity_tier || "无";
        const radios = (dictItems.length > 0
          ? dictItems
          : [
              { dict_key: "无", dict_label: "无", color_class: "ag-none" },
              { dict_key: "蓝色Pro", dict_label: "Pro", color_class: "ag-blue" },
              { dict_key: "金色Pro", dict_label: "Pro", color_class: "ag-gold" },
            ]
        )
          .map((d) => {
            const checked = d.dict_key === curVal ? "checked" : "";
            const cls = d.color_class ? `pill-${escapeHtml(d.color_class)}` : "";
            const dot = d.color_class ? `<span class="badge-dot dot-${escapeHtml(d.color_class)}"></span>` : "";
            return `
            <label class="type-radio">
              <input type="radio" name="antigravity_tier" value="${escapeHtml(d.dict_key)}" ${checked} />
              <span class="radio-pill ${cls}">${dot}${escapeHtml(d.dict_label || d.dict_key)}</span>
            </label>
          `;
          })
          .join("");

        return `
        <div class="form-group" style="margin-bottom: 14px;">
          <label>Antigravity 选项标记</label>
          <div class="health-selector" id="field-antigravity-selector">${radios}</div>
        </div>
        `;
      }

      // Custom attributes: select vs text
      if (attr.attr_type === "select") {
        const dictItems = (state.dicts || []).filter((d) => d.dict_type === attr.dict_type);
        const curVal = currentValues[attr.attr_key] || (dictItems[0]?.dict_key || "");
        const radios = dictItems
          .map((d) => {
            const checked = d.dict_key === curVal ? "checked" : "";
            return `
            <label class="type-radio">
              <input type="radio" name="${escapeHtml(attr.attr_key)}" value="${escapeHtml(d.dict_key)}" ${checked} />
              <span class="radio-pill">${d.icon ? `${escapeHtml(d.icon)} ` : ""}${escapeHtml(d.dict_label || d.dict_key)}</span>
            </label>
          `;
          })
          .join("");

        return `
        <div class="form-group" style="margin-bottom: 14px;">
          <label>${escapeHtml(attr.attr_label)}</label>
          <div class="health-selector">${radios || '<span style="color:var(--text-muted);font-size:12px;">暂无字典选项</span>'}</div>
        </div>
        `;
      } else {
        const curVal = currentValues[attr.attr_key] || "";
        return `
        <div class="form-group" style="margin-bottom: 14px;">
          <label for="field-custom-${escapeHtml(attr.attr_key)}">${escapeHtml(attr.attr_label)}</label>
          <input type="text" id="field-custom-${escapeHtml(attr.attr_key)}" name="${escapeHtml(attr.attr_key)}" class="settings-input" value="${escapeHtml(curVal)}" placeholder="请输入${escapeHtml(attr.attr_label)}" />
        </div>
        `;
      }
    })
    .join("");
}

function getCurrentDynamicFormValues() {
  const values = {};
  const container = document.getElementById("form-dynamic-attributes-container");
  if (!container) return values;

  container.querySelectorAll("input[type='radio']:checked").forEach((rad) => {
    values[rad.name] = rad.value;
  });

  container.querySelectorAll("input[type='text'], input[type='number']").forEach((inp) => {
    if (inp.name) {
      values[inp.name] = inp.value.trim();
    }
  });

  return values;
}

function onAccountTypeRadioChange(newType) {
  const currentValues = getCurrentDynamicFormValues();
  renderAccountModalDynamicFields(newType, currentValues);
  syncDomainEmailLoginHint(newType);
  state.importAccountType = (newType && newType !== "ChatGPT") ? newType : "";
  const raw = document.getElementById("smart-paste-input").value;
  if (raw.trim()) onSmartPasteInput(raw);
}

function renderCustomDrawerRows(item) {
  if (!item || !item.custom_fields) return "";
  const rows = [];
  (state.attributes || []).forEach((attr) => {
    if (!["health_status", "device_tag", "antigravity_tier"].includes(attr.attr_key) && isAttributeApplicable(attr, item.account_type)) {
      const val = item.custom_fields[attr.attr_key];
      if (val) {
        rows.push(`
          <div class="detail-row">
            <span class="detail-key">${escapeHtml(attr.attr_label)}</span>
            <span class="detail-val"><strong>${escapeHtml(val)}</strong></span>
          </div>
        `);
      }
    }
  });
  return rows.join("");
}


// ============================================================
// MAINTENANCE, WARMING, BACKUPS, CLOCK SYNC, RT/OAUTH & APPEALS
// ============================================================

function copyPlainText(val, label = "内容") {
  const text = String(val || "");
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => showToast(`📋 已复制${label}`)).catch(() => {
        _fallbackCopy(text, label);
      });
      return;
    }
  } catch (_) {}
  _fallbackCopy(text, label);
}

function _fallbackCopy(text, label) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;left:-9999px;top:-9999px;opacity:0;";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    showToast(`📋 已复制${label}`);
  } catch (_) {
    showToast("复制失败，请手动复制");
  }
}

function syncRecoveryEmailDatalist() {
  const dl = document.getElementById("recovery-email-suggestions");
  if (!dl) return;
  const list = state.topRecoveryEmails || [];
  dl.innerHTML = list
    .map((x) => `<option value="${escapeHtml(x.email)}">使用 ${x.count} 次</option>`)
    .join("");
}

async function onClickGeneratePersona() {
  const region = document.getElementById("persona-region")?.value || "US";
  const curType = getRadioType();
  const domain = curType === "Google" ? "gmail.com" : "outlook.com";
  try {
    const res = await callApi("generate_account_persona", region, domain);
    if (!res.ok || !res.persona) return;
    const p = res.persona;
    const accEl = document.getElementById("field-account");
    const pwdEl = document.getElementById("field-password");
    const regEl = document.getElementById("field-region");
    const yearEl = document.getElementById("field-year");
    const noteEl = document.getElementById("field-note");
    if (accEl && !accEl.value.trim()) accEl.value = p.suggested_email;
    if (pwdEl) pwdEl.value = p.password;
    if (regEl && !regEl.value.trim()) regEl.value = p.country;
    if (yearEl && !yearEl.value.trim()) yearEl.value = String(new Date().getFullYear());
    if (noteEl && !noteEl.value.trim()) noteEl.value = `${p.full_name} · ${p.gender_label} · ${p.birth_date}`;
    const prevEl = document.getElementById("persona-preview-text");
    if (prevEl) {
      prevEl.textContent = `${p.full_name} (${p.gender_label}) · 生日 ${p.birth_date} · 建议邮箱 ${p.suggested_email}`;
    }
    copyPlainText(`${p.full_name} / ${p.birth_date} / ${p.password}`, "拟真身份资料");
  } catch (err) {
    showToast("生成身份失败: " + err.message);
  }
}

async function onClickFillStrongPassword() {
  try {
    const res = await callApi("generate_strong_password", 16);
    if (res && res.password) {
      const pwdEl = document.getElementById("field-password");
      if (pwdEl) pwdEl.value = res.password;
      copyPlainText(res.password, "16位强密码");
    }
  } catch (err) {
    showToast("生成强密失败: " + err.message);
  }
}

async function onClickSyncGoogleTime() {
  const btn = document.getElementById("btn-sync-google-time");
  if (btn) btn.disabled = true;
  try {
    const res = await callApi("sync_google_time", "");
    if (res && res.ok) {
      state.settings = { ...(state.settings || {}), totp_offset_seconds: res.offset_seconds };
      const offsetEl = document.getElementById("lbl-totp-offset");
      if (offsetEl) {
        const off = Number(res.offset_seconds || 0);
        offsetEl.textContent = `${off > 0 ? "+" : ""}${off} 秒`;
      }
      showToast(`⏱️ 时间同步成功，已校准偏差 ${res.offset_seconds} 秒`);
      await loadDashboard();
    } else {
      showToast("同步失败: " + (res?.error || "无法连接 Google 服务器"));
    }
  } catch (err) {
    showToast("同步时间异常: " + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadBackupsList() {
  const box = document.getElementById("settings-backup-list");
  if (!box) return;
  if (!box._delegated) {
    box._delegated = true;
    box.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-restore-backup]");
      if (btn) onClickRestoreBackup(btn.getAttribute("data-restore-backup") || "");
    });
  }
  try {
    const res = await callApi("list_backups");
    const list = (res && res.backups) || [];
    if (!list.length) {
      box.innerHTML = `<div class="backup-empty">暂无历史备份文件</div>`;
      return;
    }
    box.innerHTML = list
      .map(
        (b) => `
        <div class="backup-row">
          <div class="backup-meta">
            <strong>${escapeHtml(b.filename)}</strong>
            <span>${escapeHtml(b.created_at)} · ${(Number(b.size_bytes || 0) / 1024).toFixed(1)} KB</span>
          </div>
          <button type="button" class="btn btn-secondary btn-xs" data-restore-backup="${escapeHtml(b.filename)}">
            ♻️ 恢复到此版本
          </button>
        </div>
      `
      )
      .join("");
  } catch (err) {
    box.innerHTML = `<div class="backup-empty">加载失败: ${escapeHtml(err.message)}</div>`;
  }
}

async function onClickCreateBackup() {
  try {
    const res = await callApi("create_backup", "manual");
    if (res && res.ok) {
      showToast(`💾 备份已创建: ${res.filename}`);
      await loadBackupsList();
    } else {
      showToast("创建备份失败: " + (res?.error || "未知错误"));
    }
  } catch (err) {
    showToast("备份异常: " + err.message);
  }
}

async function onClickRestoreBackup(filename) {
  if (!confirm(`确定恢复数据库到备份 [${filename}] 吗？\n系统会在恢复前自动创建一个 pre_restore 安全保护备份。`)) return;
  try {
    const res = await callApi("restore_backup", filename);
    if (res && res.ok) {
      showToast(`♻️ 已恢复到 ${res.restored}`);
      closeSettingsModal();
      await loadDashboard();
    } else {
      showToast("恢复失败: " + (res?.error || "未知错误"));
    }
  } catch (err) {
    showToast("恢复异常: " + err.message);
  }
}

// --- Quick Credential & Warming Modal ---

async function openQuickCredModal(accountId) {
  const acc = state.accounts.find((a) => a.id === accountId);
  if (!acc) return;
  state.activeQuickCredAccountId = accountId;

  document.getElementById("qc-account-id").value = String(accountId);
  const isGoogle = acc.account_type === "Google";
  const title = isGoogle ? "账号养护" : "养护与快捷改密";
  document.getElementById("qc-modal-title").textContent = `${title} · ${acc.account}`;
  document.getElementById("qc-modal-subtitle").textContent = `${acc.account_type} · ${getAccountHealthMeta(acc).label}`;
  document.getElementById("qc-generate-password").textContent = isGoogle ? "生成候选密码" : "生成密码并复制";
  const genSecretBtn = document.getElementById("qc-generate-secret");
  if (genSecretBtn) {
    genSecretBtn.textContent = "随机新密钥";
    genSecretBtn.hidden = isGoogle;
  }
  document.getElementById("qc-google-confirm-label").hidden = !isGoogle;
  document.getElementById("qc-google-confirmed").checked = false;
  renderGoogleReview(acc);

  renderQuickCredWarming(acc);
  document.getElementById("qc-scan-qr-mode").checked = !!(acc.custom_fields && acc.custom_fields.scan_qr_mode);

  const sec = acc.security || {};
  document.getElementById("qc-sec-pwd").checked = !!acc.password_modified;
  document.getElementById("qc-sec-2fa").checked = !!acc.twofa_modified;
  document.getElementById("qc-sec-recovery").checked = !!sec.recovery_modified;
  document.getElementById("qc-sec-devices").checked = !!sec.devices_cleared;

  document.getElementById("qc-password").value = acc.password || "";
  document.getElementById("qc-secret").value = acc.secret_key || "";
  document.getElementById("qc-recovery-email").value = acc.recovery_email || "";
  document.getElementById("qc-recovery-password").value = acc.recovery_password || "";
  document.getElementById("qc-note").value = acc.note || "";
  onQcSecretInput(acc.secret_key || "");

  const recoveryEmail = document.getElementById("qc-recovery-email");
  const chipsEl = document.getElementById("qc-top-recovery-chips");
  chipsEl.replaceChildren();
  for (const { email, count } of state.topRecoveryEmails.slice(0, 6)) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "recovery-chip";
    chip.title = `已使用 ${count} 次`;
    chip.textContent = `${email} (${count})`;
    chip.addEventListener("click", () => {
      if (recoveryEmail.value === email) return;
      recoveryEmail.value = email;
      recoveryEmail.dispatchEvent(new Event("input", { bubbles: true }));
    });
    chipsEl.append(chip);
  }

  document.getElementById("quick-cred-modal").style.display = "flex";
  await loadQuickCredHistory(accountId);
}

function closeQuickCredModal() {
  state.activeQuickCredAccountId = null;
  document.getElementById("quick-cred-modal").style.display = "none";
  document.getElementById("qc-history-list").replaceChildren();
  document.getElementById("qc-history-count").textContent = "";
}

function renderQuickCredWarming(account) {
  const warm = getWarmingInfo(account);
  const badge = document.getElementById("qc-warming-badge");
  badge.className = `warm-badge warm-${warm.phase}`;
  badge.textContent = `🌱 ${warm.label}`;
  document.getElementById("qc-warming-hint").textContent = warm.hint || "";
}

function renderGoogleReview(account) {
  document.getElementById("qc-google-review").hidden = account.account_type !== "Google";
  if (account.account_type !== "Google") return;
  const reviewedAt = account.custom_fields?.google_reviewed_at;
  const statusLabel = getAccountHealthMeta(account).label;
  document.getElementById("qc-modal-subtitle").textContent = `Google · ${statusLabel}`;
  document.getElementById("qc-google-review-status").textContent = statusLabel;
  document.getElementById("qc-google-review-time").textContent = reviewedAt
    ? `上次核对 ${new Date(reviewedAt).toLocaleString()}`
    : "尚无人工核对时间";
}

async function onGoogleReviewChange(select) {
  const accountId = state.activeQuickCredAccountId;
  const status = select.value;
  if (!accountId || !status) return;
  select.disabled = true;
  try {
    await onQuickSelectHealth(accountId, status);
  } finally {
    select.value = "";
    select.disabled = false;
  }
}

function openGoogleSecurityPage() {
  const accountId = state.activeQuickCredAccountId;
  if (accountId) return executeLaunchBrowser(accountId, null, "https://myaccount.google.com/security");
}

document.getElementById("quick-cred-modal").addEventListener("input", (event) => {
  if (["qc-password", "qc-secret", "qc-recovery-email", "qc-recovery-password", "qc-sec-pwd",
    "qc-sec-2fa", "qc-sec-recovery", "qc-sec-devices"].includes(event.target.id)) {
    document.getElementById("qc-google-confirmed").checked = false;
  }
});

async function loadQuickCredHistory(accountId) {
  const listEl = document.getElementById("qc-history-list");
  const countEl = document.getElementById("qc-history-count");
  const message = document.createElement("div");
  message.className = "backup-empty";
  message.textContent = "正在加载凭据历史…";
  countEl.textContent = "";
  listEl.replaceChildren(message);
  try {
    const res = await callApi("get_credential_history", accountId);
    if (!message.isConnected) return;
    const history = res?.history || [];
    countEl.textContent = `${history.length} 条记录`;
    if (!history.length) {
      message.textContent = "暂无历史变更记录（修改密码 / 2FA / 辅助邮箱后将自动归档旧凭据）";
      return;
    }

    const items = document.createDocumentFragment();
    for (const entry of history) {
      const item = document.createElement("div");
      item.className = "cred-history-item";
      item.innerHTML = `
        <div class="cred-history-meta"><span></span><span class="hist-reason"></span></div>
        <div class="cred-history-vals"></div>`;
      item.querySelector(".cred-history-meta span").textContent = entry.created_at || "";
      item.querySelector(".hist-reason").textContent = entry.reason || "update";
      const values = item.querySelector(".cred-history-vals");
      for (const [value, icon, label] of [
        [entry.old_password, "🔑", "历史密码"],
        [entry.old_secret_key, "🛡️", "历史2FA"],
        [entry.old_recovery_email, "📧", "历史辅助邮箱"],
        [entry.old_recovery_password, "🔐", "历史辅助密码"],
      ]) {
        if (!value) continue;
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "hist-chip";
        chip.title = `点击复制${label}`;
        chip.textContent = `${icon} ${value}`;
        chip.addEventListener("click", () => copyPlainText(value, label));
        values.append(chip);
      }
      items.append(item);
    }
    listEl.replaceChildren(items);
  } catch (err) {
    message.textContent = `加载失败: ${err.message}`;
  }
}

async function onQcSecretInput(val) {
  const el = document.getElementById("qc-totp-preview");
  if (!el) return;
  const cleaned = normalizeSecretString(val);
  if (!cleaned) {
    el.textContent = "实时验证码预览: 无";
    return;
  }
  try {
    const res = await callApi("preview_totp", cleaned);
    if (res && res.totp && res.totp.valid) {
      el.textContent = `✓ 当前动态码: ${formatTotpCode(res.totp.code)} (剩余 ${res.totp.remaining}s)`;
    } else {
      el.textContent = "⚠ 非标准 Base32 格式";
    }
  } catch (_) {}
}

async function onClickQcGenPassword() {
  try {
    const res = await callApi("generate_strong_password", 16);
    if (res && res.password) {
      document.getElementById("qc-password").value = res.password;
      if (state.accounts.find((account) => account.id === state.activeQuickCredAccountId)?.account_type === "Google") {
        document.getElementById("qc-google-confirmed").checked = false;
        copyPlainText(res.password, "16位候选密码（请在 Google 安全页修改生效后勾选确认再保存）");
        return;
      }
      document.getElementById("qc-sec-pwd").checked = true;
      copyPlainText(res.password, "16位强密码");
    }
  } catch (err) {
    showToast("生成密码失败: " + err.message);
  }
}

async function onClickQcGenSecret() {
  if (state.accounts.find((account) => account.id === state.activeQuickCredAccountId)?.account_type === "Google") {
    return openGoogleSecurityPage();
  }
  try {
    const res = await callApi("generate_strong_password", 16);
    if (res && res.secret_key) {
      document.getElementById("qc-secret").value = res.secret_key;
      document.getElementById("qc-sec-2fa").checked = true;
      onQcSecretInput(res.secret_key);
      copyPlainText(res.secret_key, "2FA密钥");
    }
  } catch (err) {
    showToast("生成密钥失败: " + err.message);
  }
}

async function onClickResetWarming() {
  const accountId = state.activeQuickCredAccountId;
  if (!accountId) return;
  const scanQr = document.getElementById("qc-scan-qr-mode").checked;
  try {
    const res = await callApi("reset_warming_time", accountId, scanQr, "");
    if (!res?.ok) {
      showToast(res?.error || "重置提醒失败");
      return;
    }
    showToast("已从今日重新计时");
    if (res.account && state.activeQuickCredAccountId === accountId) {
      renderQuickCredWarming(res.account);
    }
    await loadDashboard();
  } catch (err) {
    showToast("重置提醒失败: " + err.message);
  }
}

async function saveQuickCredModal(e) {
  e.preventDefault();
  const accountId = Number(document.getElementById("qc-account-id").value);
  if (!accountId) return;
  const payload = {
    password: document.getElementById("qc-password").value.trim(),
    secret_key: normalizeSecretString(document.getElementById("qc-secret").value),
    recovery_email: document.getElementById("qc-recovery-email").value.trim(),
    recovery_password: document.getElementById("qc-recovery-password").value.trim(),
    note: document.getElementById("qc-note").value.trim(),
    password_modified: document.getElementById("qc-sec-pwd").checked ? 1 : 0,
    twofa_modified: document.getElementById("qc-sec-2fa").checked ? 1 : 0,
    sec_recovery_modified: !!document.getElementById("qc-sec-recovery").checked,
    sec_devices_cleared: !!document.getElementById("qc-sec-devices").checked,
    scan_qr_mode: !!document.getElementById("qc-scan-qr-mode").checked,
    google_confirmed: document.getElementById("qc-google-confirmed").checked,
    reason: "quick_edit",
  };
  try {
    const res = await callApi("quick_update_credentials", accountId, payload);
    if (res && res.ok) {
      showToast("🛡️ 安全凭据已保存（旧凭据已自动归档历史）");
      if (state.activeQuickCredAccountId === accountId) closeQuickCredModal();
      await loadDashboard();
    } else {
      showToast("保存失败: " + (res?.error || "未知错误"));
    }
  } catch (err) {
    showToast("保存异常: " + err.message);
  }
}

// --- Antigravity / GCP RT Diagnostic & OAuth Workbench ---

function getModalAccountsFallback() {
  return (state.modalAccountsPool && state.modalAccountsPool.length) ? state.modalAccountsPool : state.accounts;
}

async function loadAllAccountsForModal() {
  const hasCustomFilter = Object.values(state.customFilters || {}).some((v) => v && v !== "All");
  const isFiltered =
    state.filterType !== "All" ||
    Boolean((state.searchQuery || "").trim()) ||
    state.statusFilter !== "All" ||
    state.healthFilter !== "All" ||
    state.antigravityFilter !== "All" ||
    state.deviceFilter !== "All" ||
    hasCustomFilter;
  if (!isFiltered && state.accounts.length) {
    state.modalAccountsPool = state.accounts.slice();
    return state.modalAccountsPool;
  }
  try {
    const res = await callApi("get_dashboard", "All", "", "All", "All", "All", "All", {});
    if (res && Array.isArray(res.accounts)) {
      state.modalAccountsPool = res.accounts;
      return state.modalAccountsPool;
    }
  } catch (_) {}
  state.modalAccountsPool = state.accounts.slice();
  return state.modalAccountsPool;
}

async function openRtOAuthModal(accountId = null) {
  const modal = document.getElementById("rt-oauth-modal");
  if (!modal) return;

  const allAccs = await loadAllAccountsForModal();
  const pool = allAccs.filter((a) => a.account_type === "Google");
  const sel = document.getElementById("rt-modal-account-select");
  if (sel) {
    sel.innerHTML = pool.length
      ? pool
          .map(
            (a) =>
              `<option value="${a.id}">${escapeHtml(a.account)} (${escapeHtml(a.account_type)}${a.has_rt ? " · 已绑RT" : ""})</option>`
          )
          .join("")
      : '<option value="">暂无 Google 账号，请先添加</option>';
    if (accountId && pool.some((a) => a.id === accountId)) {
      sel.value = String(accountId);
    } else if (pool.length) {
      sel.value = String(pool[0].id);
    }
    syncCustomSelect(sel);
  }
  const chosenId = Number(sel?.value || accountId || (pool[0] && pool[0].id) || 0);
  state.activeRtAccountId = chosenId || null;
  onRtAccountSelectChange(chosenId);
  const pre = document.getElementById("rt-probe-result");
  if (pre) pre.style.display = "none";
  modal.style.display = "flex";
}

function stopOAuthStatusPoller() {
  if (state.oauthPollTimer) {
    clearInterval(state.oauthPollTimer);
    state.oauthPollTimer = null;
  }
  state.oauthPollingAccountId = null;
}

function closeRtOAuthModal() {
  stopOAuthStatusPoller();
  const modal = document.getElementById("rt-oauth-modal");
  if (modal) modal.style.display = "none";
}

function onRtAccountSelectChange(val) {
  const accId = Number(val || 0);
  if (state.oauthPollingAccountId && accId !== state.oauthPollingAccountId) {
    stopOAuthStatusPoller();
  }
  state.activeRtAccountId = accId || null;
  const acc = getModalAccountsFallback().find((a) => a.id === accId);
  const banner = document.getElementById("rt-account-status-banner");
  const ta = document.getElementById("rt-json-textarea");
  if (!acc) {
    if (banner) banner.textContent = "请先添加一个 Google 账号";
    if (ta) ta.value = "";
    return;
  }
  const cf = acc.custom_fields || {};
  if (ta) ta.value = cf.rt_json || "";
  if (banner) {
    const proxyTxt = (acc.fp_config && acc.fp_config.proxy) ? acc.fp_config.proxy : "直连(无代理)";
    const tzTxt = (acc.fp_config && acc.fp_config.timezone) ? acc.fp_config.timezone : "默认时区";
    banner.innerHTML = `
      <span><strong>RT:</strong> ${escapeHtml(acc.rt_status || "none")}</span>
      <span><strong>GCP:</strong> ${escapeHtml(acc.gcp_status || "none")}</span>
      <span><strong>订阅:</strong> ${escapeHtml(acc.subscription_plan || "Free")}</span>
      <span><strong>代理:</strong> ${escapeHtml(proxyTxt)} (${escapeHtml(tzTxt)})</span>
      ${cf.rt_last_checked ? `<span><strong>上次探测:</strong> ${escapeHtml(cf.rt_last_checked)}</span>` : ""}
    `;
  }
}

async function onClickStartBuiltinOAuth() {
  const accId = Number(document.getElementById("rt-modal-account-select")?.value || state.activeRtAccountId || 0);
  if (!accId) {
    showToast("请先选择一个 Google 账号");
    return;
  }
  const statusEl = document.getElementById("oauth-loopback-status");
  try {
    const res = await callApi("start_builtin_oauth", accId, true, true);
    if (!res || !res.ok) {
      showToast("启动 OAuth 失败: " + (res?.error || "未知错误"));
      return;
    }
    state.oauthSessionId = res.session_id;
    state.oauthAuthUrl = res.auth_url;
    document.getElementById("btn-cancel-oauth").style.display = "inline-flex";
    document.getElementById("btn-copy-oauth-url").style.display = "inline-flex";
    if (statusEl) {
      statusEl.textContent = res.browser_launch?.ok
        ? "已请求打开授权页，正在等待 Google 授权。"
        : `浏览器启动失败（${res.browser_launch?.error || "未知错误"}）。可复制授权链接到浏览器继续。`;
    }
    if (res.browser_launch?.ok) showBrowserLaunchResult(accId, res.browser_launch);
    startOAuthStatusPoller(res.session_id, accId);
  } catch (err) {
    showToast("启动 OAuth 异常: " + err.message);
  }
}

function startOAuthStatusPoller(sessionId, accId) {
  stopOAuthStatusPoller();
  state.oauthPollingAccountId = accId;
  state.oauthPollTimer = setInterval(async () => {
    try {
      const st = await callApi("get_builtin_oauth_status", sessionId);
      const statusEl = document.getElementById("oauth-loopback-status");
      if (!st || !st.ok || st.status !== "pending") {
        stopOAuthStatusPoller();
        document.getElementById("btn-cancel-oauth").style.display = "none";
        if (st && st.status === "success") {
          if (statusEl) statusEl.textContent = `✅ OAuth 授权完成并已自动保存 RT 凭据 (${st.email || "已绑定"})`;
          showToast("✅ OAuth 授权完成，已绑定 RT 凭据！");
          await loadDashboard();
          onRtAccountSelectChange(accId);
        } else if (st) {
          if (statusEl) statusEl.textContent = `⚠️ OAuth 状态: ${st.status} ${st.error || ""}`;
        }
      }
    } catch (_) {}
  }, 1500);
}

async function onClickCancelBuiltinOAuth() {
  stopOAuthStatusPoller();
  if (state.oauthSessionId) {
    await callApi("cancel_builtin_oauth", state.oauthSessionId);
    state.oauthSessionId = "";
  }
  document.getElementById("btn-cancel-oauth").style.display = "none";
  const statusEl = document.getElementById("oauth-loopback-status");
  if (statusEl) statusEl.textContent = "已取消本地 OAuth 回调监听";
}

function onClickCopyOAuthUrl() {
  if (state.oauthAuthUrl) copyPlainText(state.oauthAuthUrl, "OAuth 授权链接");
}

async function onClickInspectRtJson() {
  const raw = document.getElementById("rt-json-textarea")?.value || "";
  const pre = document.getElementById("rt-probe-result");
  try {
    const res = await callApi("inspect_rt_json", raw);
    if (pre) {
      pre.style.display = "block";
      pre.textContent = JSON.stringify(res, null, 2);
    }
    showToast(res.ok ? "✅ RT 格式校验通过" : `❌ ${res.error || "格式无效"}`);
  } catch (err) {
    showToast("校验异常: " + err.message);
  }
}

async function onClickSaveAccountRt() {
  const accId = Number(document.getElementById("rt-modal-account-select")?.value || 0);
  const raw = document.getElementById("rt-json-textarea")?.value || "";
  if (!accId || !raw.trim()) {
    showToast("请选择账号并填入 RT 内容");
    return;
  }
  try {
    const res = await callApi("save_account_rt", accId, raw);
    if (res && res.ok) {
      showToast("💾 RT 凭据已保存到当前账号");
      await loadDashboard();
      onRtAccountSelectChange(accId);
    } else {
      showToast("保存失败: " + (res?.error || "格式无效"));
    }
  } catch (err) {
    showToast("保存失败: " + err.message);
  }
}

async function onClickBatchImportRtJson() {
  const raw = document.getElementById("rt-json-textarea")?.value || "";
  if (!raw.trim()) {
    showToast("请先粘贴 RT JSON 数组或对象");
    return;
  }
  try {
    const res = await callApi("batch_import_rt_json", raw);
    if (res && res.ok) {
      showToast(`📥 批量导入 RT 完成：新建 ${res.imported} · 更新 ${res.updated}`);
      await loadDashboard();
    } else {
      showToast("导入失败: " + (res?.error || "未能匹配账号"));
    }
  } catch (err) {
    showToast("批量导入 RT 异常: " + err.message);
  }
}

async function onClickExportRtJson() {
  try {
    const res = await callApi("export_rt_json", null);
    if (res && res.ok) {
      document.getElementById("rt-json-textarea").value = res.json_text;
      copyPlainText(res.json_text, `全部 ${res.count} 份 RT JSON`);
    } else {
      showToast(res?.error || "没有可导出的 RT 凭据");
    }
  } catch (err) {
    showToast("导出 RT 异常: " + err.message);
  }
}

async function onClickProbeGcpRt() {
  const accId = Number(document.getElementById("rt-modal-account-select")?.value || 0);
  if (!accId) return;
  const pre = document.getElementById("rt-probe-result");
  if (pre) {
    pre.style.display = "block";
    pre.textContent = "⏳ 正在通过账号专属代理连接 daily-cloudcode-pa.googleapis.com 执行 4 步探测...";
  }
  try {
    const res = await callApi("probe_account_gcp_rt", accId);
    if (pre) {
      const displayObj = { ...res };
      delete displayObj.account;
      delete displayObj.access_token;
      pre.textContent = JSON.stringify(displayObj, null, 2);
    }
    showToast(res.ok ? `🩺 探测成功：${res.subscription_plan || "Free"} · 模型 ${res.models_count || 0} 个` : `⚠️ ${res.error || "探测异常"}`);
    await loadDashboard();
    onRtAccountSelectChange(accId);
  } catch (err) {
    if (pre) pre.textContent = "探测异常: " + err.message;
  }
}

async function onClickBatchProbeGcpRt() {
  const pre = document.getElementById("rt-probe-result");
  if (pre) {
    pre.style.display = "block";
    pre.textContent = "⏳ 正在批量诊断全部已绑定 RT 的 Google 账号...";
  }
  try {
    const res = await callApi("batch_probe_gcp_rt", null);
    if (pre) pre.textContent = JSON.stringify(res, null, 2);
    showToast(res.ok ? `⚡ 批量探测完成：${res.active}/${res.total} 个 RT 有效` : (res.error || "批量探测无目标"));
    await loadDashboard();
  } catch (err) {
    if (pre) pre.textContent = "批量探测异常: " + err.message;
  }
}

async function onClickInjectAntigravity() {
  const accId = Number(document.getElementById("rt-modal-account-select")?.value || 0);
  if (!accId) return;
  try {
    const res = await callApi("inject_rt_to_antigravity", accId);
    if (res && res.ok) {
      showToast(`💉 已成功注入到 ${res.path}`);
    } else {
      showToast("注入失败: " + (res?.error || "未知错误"));
    }
  } catch (err) {
    showToast("注入异常: " + err.message);
  }
}

async function onClickProbeLiveValidation() {
  const accId = Number(document.getElementById("rt-modal-account-select")?.value || 0);
  if (!accId) return;
  showToast("🔐 正在探测实时 Google Verify 验证直链...");
  try {
    const res = await callApi("probe_live_validation", accId);
    if (res && res.target_url) {
      showToast(res.message || "已获取验证链接，正在打开隔离浏览器");
      await executeLaunchBrowser(accId, null, res.target_url);
    } else {
      showToast(res?.message || res?.error || "✓ 无需验证");
    }
  } catch (err) {
    showToast("探测验证页异常: " + err.message);
  }
}

async function onClickResetRejectedSession() {
  const accId = Number(document.getElementById("rt-modal-account-select")?.value || 0);
  if (!accId) return;
  try {
    const res = await callApi("reset_rejected_session", accId);
    if (res && res.ok) {
      const removed = (res.removed || []).length;
      showToast(`🧹 已清理异常会话缓存 (${removed} 项)，可重新打开纯净登录`);
    } else {
      showToast(res?.error || "清理异常会话失败");
    }
  } catch (err) {
    showToast("清理异常会话异常: " + err.message);
  }
}

async function onClickProbeProxyGeo() {
  const accId = Number(document.getElementById("rt-modal-account-select")?.value || 0);
  if (!accId) return;
  const pre = document.getElementById("rt-probe-result");
  if (pre) {
    pre.style.display = "block";
    pre.textContent = "⏳ 正在探测代理出口 IP 归属地与时区...";
  }
  try {
    const res = await callApi("probe_account_proxy_geo", accId, true);
    if (pre) {
      const displayObj = { ...res };
      delete displayObj.account;
      pre.textContent = JSON.stringify(displayObj, null, 2);
    }
    if (res && res.ok) {
      showToast(res.warning ? `⚠️ ${res.warning}` : `🌍 出口 ${res.country_code} (${res.ip}) · 已同步时区 ${res.timezone}`);
      await loadDashboard();
      onRtAccountSelectChange(accId);
    } else {
      showToast("代理探测失败: " + (res?.error || "超时"));
    }
  } catch (err) {
    if (pre) pre.textContent = "探测异常: " + err.message;
  }
}

// --- ChatGPT / Claude AI Credential Health Probe ---

async function onClickProbeAiAccount(accountId) {
  showToast("🩺 正在探测 AI API / Session Token...");
  try {
    const res = await callApi("probe_ai_account", accountId);
    if (res && res.ok) {
      const extra = res.models_count ? `可用模型 ${res.models_count} 个` : (res.expires_at ? `有效期至 ${res.expires_at}` : "有效");
      showToast(`✅ ${res.provider} 测活通过 (${extra})`);
    } else {
      showToast(`❌ ${res?.error || "凭据无效或已过期"}`);
    }
    await loadDashboard();
  } catch (err) {
    showToast("测活异常: " + err.message);
  }
}

// --- Google / GCP Appeal & Recovery Channels Modal ---

function isGoogleAppealCandidate(acc) {
  if (!acc || acc.account_type !== "Google") return false;
  const health = String(acc.health_status || "可用").trim();
  const gcp = String(acc.gcp_status || "none").trim();
  const rt = String(acc.rt_status || "none").trim();
  const hasValUrl = !!(acc.custom_fields && acc.custom_fields.rt_validation_url);
  return (
    health !== "可用" ||
    Boolean(acc.deactivated_date) ||
    acc.availability === "不可用" ||
    hasValUrl ||
    gcp === "suspended" ||
    gcp === "validation" ||
    gcp === "unsupported" ||
    rt === "suspended" ||
    rt === "invalid_grant" ||
    rt === "unsupported"
  );
}

async function openAppealModal(accountId = null) {
  const modal = document.getElementById("appeal-modal");
  if (!modal) return;
  if (!state.appealChannels || !state.appealChannels.length) {
    try {
      const res = await callApi("get_appeal_channels");
      if (res && res.channels) state.appealChannels = res.channels;
    } catch (_) {}
  }

  const allAccs = await loadAllAccountsForModal();
  const pool = allAccs.filter(isGoogleAppealCandidate);

  const sel = document.getElementById("appeal-account-select");
  if (sel) {
    sel.innerHTML = pool.length
      ? pool
          .map((a) => {
            const hLabel = getAccountHealthMeta(a).label;
            const gcpTag = a.gcp_status && a.gcp_status !== "none" ? ` · GCP:${a.gcp_status}` : "";
            return `<option value="${a.id}">${escapeHtml(a.account)} (${escapeHtml(hLabel)}${escapeHtml(gcpTag)})</option>`;
          })
          .join("")
      : '<option value="">暂无异常或不可用状态的 Google 账号</option>';
    if (accountId && pool.some((a) => a.id === accountId)) {
      sel.value = String(accountId);
    } else if (pool.length) {
      sel.value = String(pool[0].id);
    }
    syncCustomSelect(sel);
  }
  renderAppealValidationBanner();
  renderAppealChannelsList();
  modal.style.display = "flex";
}

function closeAppealModal() {
  const modal = document.getElementById("appeal-modal");
  if (modal) modal.style.display = "none";
}

function _bindAppealActionDelegation(container) {
  if (!container || container._delegated) return;
  container._delegated = true;
  container.addEventListener("click", (e) => {
    const openBtn = e.target.closest("[data-open-url]");
    if (openBtn) {
      onClickOpenAppealUrl(openBtn.getAttribute("data-open-url") || "");
      return;
    }
    const copyBtn = e.target.closest("[data-copy-url]");
    if (copyBtn) {
      copyPlainText(
        copyBtn.getAttribute("data-copy-url") || "",
        copyBtn.getAttribute("data-copy-label") || "链接"
      );
    }
  });
}

function renderAppealValidationBanner() {
  const banner = document.getElementById("appeal-validation-banner");
  const accId = Number(document.getElementById("appeal-account-select")?.value || 0);
  const acc = getModalAccountsFallback().find((a) => a.id === accId);
  if (!banner) return;
  _bindAppealActionDelegation(banner);
  const valUrl = acc && acc.custom_fields && acc.custom_fields.rt_validation_url;
  if (valUrl) {
    banner.style.display = "flex";
    banner.innerHTML = `
      <div>
        <strong>⚠️ 检测到该账号有专属风控验证直链 (VALIDATION_REQUIRED)</strong>
        <div class="appeal-val-url">${escapeHtml(valUrl)}</div>
      </div>
      <div style="display:flex;gap:8px;">
        <button type="button" class="btn btn-primary btn-xs" data-open-url="${escapeHtml(valUrl)}">打开验证页面</button>
        <button type="button" class="btn btn-secondary btn-xs" data-copy-url="${escapeHtml(valUrl)}" data-copy-label="验证链接">📋 复制</button>
      </div>
    `;
  } else {
    banner.style.display = "none";
  }
}

function renderAppealChannelsList() {
  const box = document.getElementById("appeal-channels-list");
  if (!box) return;
  _bindAppealActionDelegation(box);
  const channels = state.appealChannels || [];
  box.innerHTML = channels
    .map(
      (ch) => `
      <div class="appeal-channel-card">
        <div class="appeal-ch-head">
          <div>
            <span class="appeal-ch-cat">${escapeHtml(ch.category)}</span>
            <h4>${escapeHtml(ch.title)}</h4>
          </div>
          <span class="appeal-ch-badge">${escapeHtml(ch.badge)}</span>
        </div>
        <p class="appeal-ch-summary">${escapeHtml(ch.summary)}</p>
        <ul class="appeal-ch-steps">
          ${(ch.steps || []).map((s) => `<li>${escapeHtml(s)}</li>`).join("")}
        </ul>
        <div class="appeal-ch-actions">
          <button type="button" class="btn btn-primary btn-xs" data-open-url="${escapeHtml(ch.url)}">使用账号专属浏览器打开</button>
          <button type="button" class="btn btn-ghost btn-xs" data-copy-url="${escapeHtml(ch.url)}" data-copy-label="申诉链接">📋 复制链接</button>
        </div>
      </div>
    `
    )
    .join("");
}

async function onClickOpenAppealUrl(url) {
  const accId = Number(document.getElementById("appeal-account-select")?.value || 0);
  if (!accId) {
    showToast("暂无或未选择异常状态的 Google 账号。");
    return;
  }
  try {
    const account = getModalAccountsFallback().find((item) => item.id === accId);
    const res = await callApi("launch_browser", accId, url);
    if (res && res.ok) {
      showBrowserLaunchResult(accId, res, account);
    } else {
      showToast("打开失败: " + (res?.error || "未知错误"));
    }
  } catch (err) {
    showToast("打开浏览器异常: " + err.message);
  }
}
