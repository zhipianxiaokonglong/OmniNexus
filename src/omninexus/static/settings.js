"use strict";

const $ = (id) => document.getElementById(id);
const S = {
  custom: [],
  builtin: [],
  models: [],
  presets: {},
  defaults: null,
  editing: null,
  formModels: [],
  migration: [],
  migrationSelected: new Set(),
};

const STATUS_LABELS = {
  success: "成功",
  unverified: "未验证",
  auth_failed: "鉴权失败",
  unreachable: "地址不可达",
  model_not_found: "模型不存在",
  timeout: "超时",
};
const LEVEL_LABELS = { reachability: "地址", models: "模型列表", model: "模型聊天" };
const PRESET_LABELS = {
  "text-basic": "文本 32K",
  "text-large": "文本 128K",
  vision: "视觉 128K",
  "local-small": "本地 8K",
  custom: "自定义",
};

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  if (response.status === 204) return null;
  let payload = null;
  try { payload = await response.json(); } catch { /* ignore */ }
  if (!response.ok) {
    const error = new Error(payload?.error?.message || `请求失败（${response.status}）`);
    error.code = payload?.error?.code;
    error.details = payload?.error?.details;
    throw error;
  }
  return payload;
}

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.hidden = true; }, 5000);
}

function providerKey(kind, id) { return `${kind}:${id}`; }

function findProvider(kind, id) {
  return S.models.find((model) => model.providerKind === kind && model.providerId === id) || null;
}

function providerModels(kind, id) {
  return S.models.filter((model) => model.providerKind === kind && model.providerId === id);
}

function allProviders() {
  const map = new Map();
  for (const model of S.models) {
    const key = providerKey(model.providerKind, model.providerId);
    if (!map.has(key)) {
      map.set(key, {
        kind: model.providerKind,
        id: model.providerId,
        name: model.provider_label || model.provider,
        models: [],
      });
    }
    map.get(key).models.push(model);
  }
  return [...map.values()];
}

async function load() {
  const [custom, builtin, models, defaults] = await Promise.all([
    api("/v1/custom-providers"),
    api("/v1/providers"),
    api("/v1/models"),
    api("/v1/provider-defaults"),
  ]);
  S.custom = custom.providers || [];
  S.builtin = builtin.providers || [];
  S.models = models.models || [];
  S.presets = custom.capabilityPresets || {};
  S.defaults = defaults.defaults || null;
  $("storage-warning").textContent = custom.storageWarning || "";
  renderBuiltins();
  renderCustom();
  renderDefaults();
  await loadMigration();
}

function renderBuiltins() {
  const list = $("builtin-list");
  list.innerHTML = "";
  for (const provider of S.builtin) {
    const models = providerModels("builtin", provider.name);
    const card = document.createElement("div");
    card.className = `provider-card${provider.configured ? "" : " disabled"}`;
    const head = document.createElement("div");
    head.className = "p-head";
    head.innerHTML = `<strong>${escapeHtml(provider.label)}</strong>
      <span class="badge ${provider.configured ? "ok" : "off"}">${provider.configured ? "已配置" : "未配置"}</span>`;
    const url = document.createElement("div");
    url.className = "p-url";
    url.textContent = provider.base_url || "local";
    const meta = document.createElement("div");
    meta.className = "p-meta";
    meta.textContent = `${provider.type} · ${models.length} 个模型 · ${
      provider.requires_key ? `密钥：${provider.api_key_env}` : "本地 / 无需密钥"
    }`;
    card.append(head, url, meta);
    list.appendChild(card);
  }
}

function renderCustom() {
  const list = $("custom-list");
  list.innerHTML = "";
  $("custom-empty").hidden = S.custom.length > 0;
  for (const provider of S.custom) {
    const card = document.createElement("div");
    const isDefault = S.defaults
      && S.defaults.providerKind === "custom"
      && S.defaults.providerId === provider.id;
    card.className = `provider-card${provider.enabled ? "" : " disabled"}${isDefault ? " is-default" : ""}`;
    const head = document.createElement("div");
    head.className = "p-head";
    head.innerHTML = `<strong>${escapeHtml(provider.name)}</strong>
      <span class="badge ${provider.enabled ? "ok" : "off"}">${provider.enabled ? "启用" : "禁用"}</span>`;
    const url = document.createElement("div");
    url.className = "p-url";
    url.textContent = provider.baseUrl;
    const meta = document.createElement("div");
    meta.className = "p-meta";
    const verifications = provider.models.map((model) => model.verification || "unverified");
    const verifiedCount = verifications.filter((value) => value === "text_verified" || value === "vision_verified").length;
    meta.textContent = `${provider.models.length} 个模型 · ${verifiedCount} 个已验证 · ${provider.hasApiKey ? provider.apiKeyMasked : "无 Key"}${isDefault ? " · 默认" : ""}`;
    const actions = document.createElement("div");
    actions.className = "p-actions";
    actions.append(
      actionButton("编辑", () => openForm(provider)),
      actionButton("测试", () => testSaved(provider)),
      actionButton(provider.enabled ? "禁用" : "启用", () => toggleProvider(provider)),
      actionButton("删除", () => deleteProvider(provider), true),
    );
    card.append(head, url, meta, actions);
    list.appendChild(card);
  }
}

function actionButton(label, handler, danger = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = danger ? "secondary danger-text" : "secondary";
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function renderDefaults() {
  const providerSelect = $("default-provider");
  providerSelect.innerHTML = "";
  const providers = allProviders();
  for (const provider of providers) {
    const option = document.createElement("option");
    option.value = providerKey(provider.kind, provider.id);
    option.textContent = `${provider.name}${provider.kind === "builtin" ? " · 内置" : ""}`;
    providerSelect.appendChild(option);
  }
  if (S.defaults) {
    providerSelect.value = providerKey(S.defaults.providerKind, S.defaults.providerId);
  }
  renderDefaultModels();
  providerSelect.onchange = renderDefaultModels;
}

function renderDefaultModels() {
  const [kind, ...idParts] = ($("default-provider").value || "").split(":");
  const id = idParts.join(":");
  const select = $("default-model");
  select.innerHTML = "";
  for (const model of providerModels(kind, id)) {
    const option = document.createElement("option");
    option.value = model.model;
    option.textContent = `${model.label}${model.available ? "" : "（不可用）"}`;
    option.disabled = !model.available;
    select.appendChild(option);
  }
  if (S.defaults && S.defaults.providerKind === kind && S.defaults.providerId === id) {
    select.value = S.defaults.model;
  }
}

function openForm(provider = null) {
  S.editing = provider;
  S.formModels = [];
  $("provider-form-card").hidden = false;
  $("form-title").textContent = provider ? `编辑提供方 · ${provider.name}` : "添加自定义提供方";
  $("provider-name").value = provider?.name || "";
  $("provider-base-url").value = provider?.baseUrl || "";
  $("provider-api-key").value = "";
  $("provider-api-key").placeholder = provider?.hasApiKey
    ? `已保存 ${provider.apiKeyMasked}，留空保持不变`
    : "sk-…（本地无鉴权可留空）";
  $("api-key-help").textContent = provider?.hasApiKey
    ? "留空保留已保存 Key；输入新值会替换；勾选下方可清除。"
    : "保存后仅显示掩码，不会回显完整 Key。";
  $("clear-key-wrap").hidden = !provider?.hasApiKey;
  $("provider-clear-key").checked = false;
  $("provider-headers").value = provider ? JSON.stringify(provider.headers || {}, null, 2) : "";
  $("provider-timeout").value = provider?.timeoutSeconds || 10;
  $("provider-enabled").checked = provider ? provider.enabled : true;
  $("base-url-suggest").hidden = true;
  $("test-panel").hidden = true;
  $("model-rows").innerHTML = "";
  if (provider?.models?.length) {
    for (const model of provider.models) addModelRow(model);
  } else {
    addModelRow();
  }
  refreshDefaultModelOptions(provider?.defaultModel);
  updateUrlPreview();
  $("provider-form-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

function addModelRow(model = null) {
  const row = document.createElement("div");
  row.className = "model-row";
  const name = model?.name || "";
  const label = model?.label || "";
  const caps = model?.capabilities || S.presets["text-basic"] || {
    inputModalities: ["text"], contextWindow: 32768, maxOutputTokens: 2048,
  };
  row.innerHTML = `
    <label class="field"><span>模型名</span><input class="m-name" value="${escapeHtml(name)}" placeholder="gpt-4o-mini"></label>
    <label class="field"><span>显示名</span><input class="m-label" value="${escapeHtml(label)}" placeholder="可选"></label>
    <label class="field"><span>能力预设</span><select class="m-preset">
      ${Object.entries(PRESET_LABELS).map(([key, value]) => `<option value="${key}">${value}</option>`).join("")}
    </select></label>
    <label class="field"><span>上下文</span><input class="m-context" type="number" min="512" value="${caps.contextWindow || 32768}"></label>
    <label class="field"><span>最大输出</span><input class="m-output" type="number" min="1" value="${caps.maxOutputTokens || 2048}"></label>
    <label class="check"><input class="m-vision" type="checkbox" ${caps.inputModalities?.includes("image") ? "checked" : ""}> 视觉</label>
    <button type="button" class="remove-model" title="删除">×</button>`;
  const preset = row.querySelector(".m-preset");
  const vision = row.querySelector(".m-vision");
  preset.addEventListener("change", () => {
    const value = S.presets[preset.value];
    if (!value && preset.value !== "custom") return;
    if (preset.value !== "custom") {
      row.querySelector(".m-context").value = value.contextWindow;
      row.querySelector(".m-output").value = value.maxOutputTokens;
      vision.checked = value.inputModalities.includes("image");
    }
  });
  row.querySelector(".remove-model").addEventListener("click", () => {
    row.remove();
    if (!$("model-rows").children.length) addModelRow();
    refreshDefaultModelOptions();
  });
  row.querySelector(".m-name").addEventListener("input", () => refreshDefaultModelOptions());
  $("model-rows").appendChild(row);
  refreshDefaultModelOptions();
}

function refreshDefaultModelOptions(preferred = null) {
  const select = $("provider-default-model");
  if (!select) return;
  const current = preferred || select.value;
  select.innerHTML = "";
  for (const row of $("model-rows").children) {
    const name = row.querySelector(".m-name").value.trim();
    if (!name) continue;
    const option = document.createElement("option");
    option.value = name;
    option.textContent = row.querySelector(".m-label").value.trim() || name;
    option.selected = name === current;
    select.appendChild(option);
  }
  if (!select.options.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "先填写模型名";
    select.appendChild(option);
  }
}

function readFormModels() {
  const models = [];
  for (const row of $("model-rows").children) {
    const name = row.querySelector(".m-name").value.trim();
    if (!name) continue;
    const vision = row.querySelector(".m-vision").checked;
    models.push({
      name,
      label: row.querySelector(".m-label").value.trim() || name,
      capabilities: {
        inputModalities: vision ? ["text", "image"] : ["text"],
        contextWindow: Number(row.querySelector(".m-context").value) || 32768,
        maxOutputTokens: Number(row.querySelector(".m-output").value) || 2048,
        supportsStreaming: true,
      },
    });
  }
  return models;
}

function parseHeaders() {
  const raw = $("provider-headers").value.trim();
  if (!raw) return {};
  let value;
  try { value = JSON.parse(raw); } catch { throw new Error("请求头必须是合法 JSON 对象。"); }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("请求头必须是 JSON 对象。");
  }
  return value;
}

function updateUrlPreview() {
  const raw = $("provider-base-url").value.trim().replace(/\/+$/, "");
  $("url-preview").textContent = raw ? `${raw}/chat/completions` : "https://api.example.com/v1/chat/completions";
}

function maybeSuggestV1() {
  const raw = $("provider-base-url").value.trim();
  if (!raw) return;
  try {
    const url = new URL(raw);
    if ((url.pathname === "" || url.pathname === "/") && !/\/v1\/?$/.test(raw)) {
      $("base-url-suggestion").textContent = `${raw.replace(/\/+$/, "")}/v1`;
      $("base-url-suggest").hidden = false;
      return;
    }
  } catch { /* handled on save */ }
  $("base-url-suggest").hidden = true;
}

function formPayload() {
  const name = $("provider-name").value.trim();
  const baseUrl = $("provider-base-url").value.trim().replace(/\/+$/, "");
  if (!name) throw new Error("请填写提供方名称。");
  if (!baseUrl) throw new Error("请填写 Base URL。");
  if (!/^https?:\/\//i.test(baseUrl)) throw new Error("Base URL 必须是 http(s) 地址。");
  const models = readFormModels();
  if (!models.length) throw new Error("至少添加一个模型。");
  const payload = {
    name,
    baseUrl,
    headers: parseHeaders(),
    protocol: "openai-compatible",
    timeoutSeconds: Number($("provider-timeout").value) || 10,
    enabled: $("provider-enabled").checked,
    models,
    defaultModel: $("provider-default-model").value || models[0].name,
  };
  const key = $("provider-api-key").value.trim();
  if (key) payload.apiKey = key;
  if (S.editing && $("provider-clear-key").checked) payload.clearApiKey = true;
  return payload;
}

async function saveProvider() {
  try {
    const payload = formPayload();
    if (S.editing) {
      await api(`/v1/custom-providers/${S.editing.id}`, {
        method: "PUT", body: JSON.stringify(payload),
      });
      toast("已保存提供方");
    } else {
      await api("/v1/custom-providers", { method: "POST", body: JSON.stringify(payload) });
      toast("已添加提供方");
    }
    $("provider-form-card").hidden = true;
    await load();
  } catch (error) {
    toast(error.message, true);
  }
}

async function discover() {
  try {
    const payload = {
      baseUrl: $("provider-base-url").value.trim().replace(/\/+$/, ""),
      headers: parseHeaders(),
      timeoutSeconds: Number($("provider-timeout").value) || 10,
    };
    const key = $("provider-api-key").value.trim();
    if (key) payload.apiKey = key;
    if (S.editing) payload.id = S.editing.id;
    const path = S.editing
      ? `/v1/custom-providers/${S.editing.id}/discover-models`
      : "/v1/custom-providers/discover-models";
    const data = await api(path, { method: "POST", body: JSON.stringify(payload) });
    const existing = new Set(
      [...$("model-rows").children].map((row) => row.querySelector(".m-name").value.trim()).filter(Boolean),
    );
    let added = 0;
    for (const name of data.models || []) {
      if (!existing.has(name)) { addModelRow({ name, capabilities: S.presets["text-basic"] }); added += 1; }
    }
    toast(added ? `已添加 ${added} 个模型；请测试连接` : "没有发现新模型；可手动填写");
  } catch (error) {
    toast(error.message, true);
  }
}

function renderTestResult(result) {
  $("test-panel").hidden = false;
  const badge = $("test-status");
  badge.textContent = STATUS_LABELS[result.status] || result.status;
  badge.className = `badge ${result.status === "success" ? "ok" : result.status === "unverified" ? "" : "off"}`;
  const levels = $("test-levels");
  levels.innerHTML = "";
  for (const level of result.levels || []) {
    const el = document.createElement("span");
    const cls = level.status === "ok" ? "ok" : level.status === "unverified" ? "warn" : "fail";
    el.className = `test-level ${cls}`;
    el.textContent = `${LEVEL_LABELS[level.level] || level.level} · ${level.status}${
      level.latencyMs != null ? ` · ${level.latencyMs}ms` : ""
    }`;
    levels.appendChild(el);
  }
  $("test-message").textContent = result.message || "";
  const multimodal = result.multimodal || {};
  $("test-multimodal").textContent = multimodal.status === "verified"
    ? "多模态探测：通过"
    : multimodal.status === "failed"
      ? "多模态探测：失败（可能不支持图片）"
      : "";
}

async function testProvider() {
  try {
    const payload = {
      baseUrl: $("provider-base-url").value.trim().replace(/\/+$/, ""),
      headers: parseHeaders(),
      timeoutSeconds: Number($("provider-timeout").value) || 10,
      model: [...$("model-rows").children][0]?.querySelector(".m-name").value.trim() || null,
      probeMultimodal: [...$("model-rows").children].some((row) => row.querySelector(".m-vision").checked),
    };
    const key = $("provider-api-key").value.trim();
    if (key) payload.apiKey = key;
    if (S.editing) payload.id = S.editing.id;
    const path = S.editing
      ? `/v1/custom-providers/${S.editing.id}/test`
      : "/v1/custom-providers/test";
    const data = await api(path, { method: "POST", body: JSON.stringify(payload) });
    renderTestResult(data.result);
    if (S.editing) await load();
  } catch (error) {
    toast(error.message, true);
  }
}

async function testSaved(provider) {
  try {
    const vision = provider.models.some((model) => model.capabilities.inputModalities.includes("image"));
    const data = await api(`/v1/custom-providers/${provider.id}/test`, {
      method: "POST",
      body: JSON.stringify({
        model: provider.defaultModel,
        probeMultimodal: vision,
        timeoutSeconds: provider.timeoutSeconds,
      }),
    });
    toast(`测试：${STATUS_LABELS[data.result.status] || data.result.status}`);
    await load();
  } catch (error) { toast(error.message, true); }
}

async function toggleProvider(provider) {
  try {
    await api(`/v1/custom-providers/${provider.id}/enabled`, {
      method: "PATCH", body: JSON.stringify({ enabled: !provider.enabled }),
    });
    toast(provider.enabled ? "已禁用提供方" : "已启用提供方");
    await load();
  } catch (error) { toast(error.message, true); }
}

async function deleteProvider(provider) {
  if (!confirm(`删除提供方“${provider.name}”？历史会话会保留并标记不可用。`)) return;
  try {
    await api(`/v1/custom-providers/${provider.id}`, { method: "DELETE" });
    toast("已删除提供方");
    await load();
    return;
  } catch (error) {
    if (error.code === "PROVIDER_IN_USE") {
      if (!confirm(error.message + "\n确认删除？")) return;
      try {
        await api(`/v1/custom-providers/${provider.id}?confirm=true`, { method: "DELETE" });
        toast("已删除提供方；历史会话已标记不可用");
        await load();
        return;
      } catch (inner) { toast(inner.message, true); return; }
    }
    toast(error.message, true);
  }
}

async function saveDefaults() {
  const [kind, ...idParts] = ($("default-provider").value || "").split(":");
  const model = $("default-model").value;
  if (!kind || !idParts.join(":") || !model) { toast("请选择默认提供方和模型", true); return; }
  try {
    await api("/v1/provider-defaults", {
      method: "PUT",
      body: JSON.stringify({ providerKind: kind, providerId: idParts.join(":"), model }),
    });
    toast("默认提供方/模型已保存");
    await load();
  } catch (error) { toast(error.message, true); }
}

async function importConfig(file) {
  if (!file) return;
  try {
    const text = await file.text();
    const payload = JSON.parse(text);
    const data = await api("/v1/provider-config/import", {
      method: "POST", body: JSON.stringify(payload),
    });
    toast(data.note || `已导入 ${data.imported} 个提供方`);
    await load();
  } catch (error) { toast(error.message, true); }
}

async function loadMigration() {
  try {
    const data = await api("/v1/provider-migration/candidates");
    S.migration = data.sessions || [];
    S.migrationSelected.clear();
    renderMigration();
  } catch { /* optional */ }
}

function renderMigration() {
  const list = $("migration-list");
  list.innerHTML = "";
  if (!S.migration.length) {
    list.innerHTML = '<div class="empty">没有不可用提供方的历史会话。</div>';
    $("migration-actions").hidden = true;
    return;
  }
  $("migration-actions").hidden = false;
  for (const session of S.migration) {
    const row = document.createElement("label");
    row.className = "migration-item";
    row.innerHTML = `<input type="checkbox" value="${escapeHtml(session.id)}">
      <span>${escapeHtml(session.title || "未命名对话")}</span>
      <span class="m-meta">${escapeHtml(session.reason || "")} · ${escapeHtml(session.model || "")}</span>`;
    row.querySelector("input").addEventListener("change", (event) => {
      if (event.target.checked) S.migrationSelected.add(session.id);
      else S.migrationSelected.delete(session.id);
    });
    list.appendChild(row);
  }
  const providerSelect = $("migration-provider");
  providerSelect.innerHTML = "";
  for (const provider of allProviders()) {
    const option = document.createElement("option");
    option.value = providerKey(provider.kind, provider.id);
    option.textContent = provider.name;
    providerSelect.appendChild(option);
  }
  providerSelect.onchange = renderMigrationModels;
  renderMigrationModels();
}

function renderMigrationModels() {
  const [kind, ...idParts] = ($("migration-provider").value || "").split(":");
  const select = $("migration-model");
  select.innerHTML = "";
  for (const model of providerModels(kind, idParts.join(":"))) {
    if (!model.available) continue;
    const option = document.createElement("option");
    option.value = model.model;
    option.textContent = model.label;
    select.appendChild(option);
  }
}

async function applyMigration() {
  if (!S.migrationSelected.size) { toast("请先勾选要迁移的会话", true); return; }
  const [kind, ...idParts] = ($("migration-provider").value || "").split(":");
  const model = $("migration-model").value;
  if (!kind || !idParts.join(":") || !model) { toast("请选择目标提供方和模型", true); return; }
  if (!confirm(`将 ${S.migrationSelected.size} 个会话的后续模型切换为目标？历史消息不会改变。`)) return;
  try {
    const data = await api("/v1/provider-migration/apply", {
      method: "POST",
      body: JSON.stringify({
        sessionIds: [...S.migrationSelected],
        targetProviderKind: kind,
        targetProviderId: idParts.join(":"),
        targetModel: model,
      }),
    });
    toast(`已迁移 ${data.updated} 个会话`);
    await loadMigration();
  } catch (error) { toast(error.message, true); }
}

function bind() {
  $("add-provider").addEventListener("click", () => openForm());
  $("close-form").addEventListener("click", () => { $("provider-form-card").hidden = true; });
  $("add-model-row").addEventListener("click", () => addModelRow());
  $("discover-models").addEventListener("click", discover);
  $("test-provider").addEventListener("click", testProvider);
  $("save-provider").addEventListener("click", saveProvider);
  $("save-defaults").addEventListener("click", saveDefaults);
  $("import-config").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", (event) => importConfig(event.target.files?.[0]));
  $("refresh-migration").addEventListener("click", loadMigration);
  $("apply-migration").addEventListener("click", applyMigration);
  $("provider-base-url").addEventListener("input", updateUrlPreview);
  $("provider-base-url").addEventListener("blur", maybeSuggestV1);
  $("accept-v1").addEventListener("click", () => {
    $("provider-base-url").value = $("base-url-suggestion").textContent;
    $("base-url-suggest").hidden = true;
    updateUrlPreview();
  });
  $("reject-v1").addEventListener("click", () => { $("base-url-suggest").hidden = true; });
}

document.addEventListener("DOMContentLoaded", async () => {
  bind();
  try { await load(); }
  catch (error) { toast(error.message, true); }
});
