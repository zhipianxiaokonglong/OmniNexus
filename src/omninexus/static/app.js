"use strict";

const $ = (id) => document.getElementById(id);

const EMOTION_LABELS = {
  joy: "喜悦", sadness: "悲伤", anger: "愤怒", fear: "担忧", surprise: "惊讶",
  confusion: "困惑", frustration: "挫败", gratitude: "感谢", neutral: "中性",
};
const TYPE_LABELS = {
  text: "文本", code: "代码", list: "列表", table: "表格", json: "JSON", mixed: "混合",
};
const INTENT_LABELS = { reply: "回复", tool: "工具/检索", linkage: "关联" };

const state = {
  session: null,
  messages: [],
  models: [],
  profiles: [],
  settings: null,
  currentModel: null,
  currentPrompt: "default",
  webSearch: false,
  streaming: false,
  controller: null,
  attachment: null,
  editingProfile: null,
  renderQueued: false,
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function markdownish(text) {
  let out = escapeHtml(text);
  out = out.replace(/```([A-Za-z0-9_+-]*)\n?([\s\S]*?)```/g, (_, _lang, code) => `<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
  out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(https?:\/\/[^\s<"]+)/g, (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
  const parts = out.split(/\n{2,}/).filter((part) => part.trim() !== "");
  return parts.map((part) => `<p>${part.replace(/\n/g, "<br>")}</p>`).join("");
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  if (response.status === 204) return null;
  let payload = null;
  try { payload = await response.json(); } catch { /* ignore */ }
  if (!response.ok) {
    const message = payload?.error?.message || `请求失败（${response.status}）`;
    const error = new Error(message);
    error.code = payload?.error?.code;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function toast(message, isError = false) {
  const element = $("toast");
  element.textContent = message;
  element.classList.toggle("error", Boolean(isError));
  element.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { element.hidden = true; }, 4200);
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function shortDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function activeModel() {
  return state.models.find((item) => item.id === state.currentModel) || null;
}

function updateComposerHint() {
  const model = activeModel();
  if (!state.models.length) {
    $("composer-hint").textContent = "未加载模型";
    return;
  }
  if (!model) {
    $("composer-hint").textContent = "未选择模型";
    return;
  }
  if (!model.available) {
    $("composer-hint").textContent = "所选模型未配置密钥";
    return;
  }
  const caps = model.input_modalities?.includes("image") ? "文本 + 图像" : "文本";
  $("composer-hint").textContent = `${model.provider_label || model.provider} · ${caps}`;
}

async function loadSettings() {
  const data = await api("/v1/settings");
  state.settings = data;
  state.models = data.models || [];
  state.profiles = data.prompt_profiles || [];
  if (!state.currentModel || !state.models.some((item) => item.id === state.currentModel)) {
    const preferred = state.models.find((item) => item.id === data.default_model && item.available)
      || state.models.find((item) => item.available)
      || state.models.find((item) => item.id === data.default_model)
      || state.models[0];
    state.currentModel = preferred?.id || null;
  }
  if (!state.profiles.some((item) => item.id === state.currentPrompt)) {
    state.currentPrompt = data.default_prompt_profile || "default";
  }
  renderModelOptions();
  renderPromptOptions();
  updateSearchAvailability();
  updateComposerHint();
}

function renderModelOptions() {
  const select = $("model-select");
  select.innerHTML = "";
  if (!state.models.length) {
    const option = document.createElement("option");
    option.textContent = "无可用模型";
    option.value = "";
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const groups = new Map();
  for (const model of state.models) {
    const key = model.provider_label || model.provider;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(model);
  }
  for (const [label, models] of groups) {
    const group = document.createElement("optgroup");
    group.label = label;
    for (const model of models) {
      const option = document.createElement("option");
      option.value = model.id;
      option.textContent = `${model.label}${model.available ? "" : "（未配置）"}`;
      option.disabled = !model.available;
      option.selected = model.id === state.currentModel;
      group.appendChild(option);
    }
    select.appendChild(group);
  }
  if (select.value !== state.currentModel) {
    const selected = Array.from(select.options).find((option) => option.value === state.currentModel);
    if (selected) selected.selected = true;
  }
}

function renderPromptOptions() {
  const select = $("prompt-select");
  select.innerHTML = "";
  for (const profile of state.profiles) {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.name;
    option.selected = profile.id === state.currentPrompt;
    select.appendChild(option);
  }
}

function updateSearchAvailability() {
  const search = state.settings?.search;
  const checkbox = $("search-toggle");
  const available = Boolean(search?.available);
  checkbox.disabled = !available;
  checkbox.checked = Boolean(state.webSearch && available);
  checkbox.parentElement.title = available
    ? `搜索源：${search.provider}`
    : "联网搜索未配置（config/default.yaml → search）";
}

async function loadSessions() {
  try {
    const data = await api("/v1/sessions?limit=100");
    const list = $("session-list");
    list.innerHTML = "";
    const sessions = data.sessions || [];
    $("session-count").textContent = sessions.length ? String(sessions.length) : "";
    for (const session of sessions) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `session-item${state.session?.id === session.id ? " active" : ""}`;
      const title = document.createElement("span");
      title.className = "s-title";
      title.textContent = session.title || "未命名对话";
      const meta = document.createElement("span");
      meta.className = "s-meta";
      meta.textContent = `${session.message_count || 0} 条 · ${shortDate(session.updated_at)}`;
      button.append(title, meta);
      button.addEventListener("click", () => openSession(session.id));
      list.appendChild(button);
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function openSession(sessionId) {
  try {
    const data = await api(`/v1/sessions/${sessionId}`);
    state.session = data.session;
    const storedCitations = data.citations || [];
    state.messages = (data.messages || []).map((message) => ({
      ...message,
      tags: message.metadata?.tags || null,
      provider: message.metadata?.provider || null,
      model: message.metadata?.model || null,
      citations: storedCitations.filter(
        (citation) => citation.event_id === message.metadata?.output_event_id,
      ),
      pending: false,
    }));
    state.currentModel = data.session.model || state.currentModel;
    state.currentPrompt = data.session.prompt_profile || state.currentPrompt;
    state.webSearch = Boolean(data.session.web_search);
    renderModelOptions();
    renderPromptOptions();
    updateSearchAvailability();
    renderMessages();
    updateSessionHeader();
    $("sidebar").classList.remove("open");
  } catch (error) {
    toast(error.message, true);
  }
}

function newChat() {
  if (state.streaming) stopStreaming();
  state.session = null;
  state.messages = [];
  state.attachment = null;
  clearAttachment();
  updateSessionHeader();
  renderMessages();
  renderSessionsActive();
  $("message-input").focus();
  $("sidebar").classList.remove("open");
}

function renderSessionsActive() {
  document.querySelectorAll(".session-item").forEach((item) => item.classList.remove("active"));
}

function updateSessionHeader() {
  $("session-title").textContent = state.session?.title || "新对话";
  const hasSession = Boolean(state.session);
  $("export-session").disabled = !hasSession;
  $("delete-session").disabled = !hasSession;
}

async function ensureSession() {
  if (state.session) return state.session;
  const model = state.currentModel || state.settings?.default_model;
  if (!model) throw new Error("没有可用模型。请先在 .env 配置厂商密钥。");
  const data = await api("/v1/sessions", {
    method: "POST",
    body: JSON.stringify({
      model,
      prompt_profile: state.currentPrompt,
      web_search: state.webSearch,
    }),
  });
  state.session = data.session;
  updateSessionHeader();
  await loadSessions();
  return state.session;
}

/* ---------------- chat ---------------- */
function renderMessages() {
  const container = $("messages");
  container.innerHTML = "";
  $("welcome").style.display = state.messages.length ? "none" : "block";
  for (const message of state.messages) container.appendChild(renderMessage(message));
  scrollToBottom();
}

function renderMessage(message) {
  const wrapper = document.createElement("article");
  wrapper.className = `message ${message.role}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = message.role === "user" ? "你" : "AI";
  const bubble = document.createElement("div");
  bubble.className = "bubble";

  const head = document.createElement("div");
  head.className = "bubble-head";
  const role = document.createElement("span");
  role.className = "role-name";
  role.textContent = message.role === "user" ? "你" : "OmniNexus";
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = formatTime(message.created_at);
  head.append(role, time);
  if (message.role === "assistant" && message.model) {
    const model = document.createElement("span");
    model.className = "time";
    model.textContent = message.model;
    head.appendChild(model);
  }
  bubble.appendChild(head);

  if (message.role === "user" && message.parts?.some((part) => part.modality === "image")) {
    const chip = document.createElement("div");
    chip.className = "attachment-chip";
    chip.textContent = "▧ 图片附件";
    bubble.appendChild(chip);
  }

  const body = document.createElement("div");
  body.className = "message-text";
  if (message.error) {
    body.innerHTML = `<span class="error-text">${escapeHtml(message.error)}</span>`;
  } else if (message.pending && !message.content) {
    body.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
  } else if (message.role === "assistant") {
    body.innerHTML = markdownish(message.content || "");
  } else {
    body.textContent = message.content || "";
  }
  bubble.appendChild(body);

  if (message.role === "assistant" && message.tags) {
    const tags = document.createElement("div");
    tags.className = "tags";
    tags.append(
      makeTag("emotion", `情绪 · ${EMOTION_LABELS[message.tags.emotion] || message.tags.emotion}`),
      makeTag("type", `类型 · ${TYPE_LABELS[message.tags.type] || message.tags.type}`),
      makeTag("intent", `意图 · ${INTENT_LABELS[message.tags.intent] || message.tags.intent}`),
    );
    bubble.appendChild(tags);
  }

  if (message.role === "assistant" && message.citations?.length) {
    const block = document.createElement("div");
    block.className = "citations";
    const label = document.createElement("strong");
    label.textContent = "引用来源";
    block.appendChild(label);
    for (const citation of message.citations) {
      const link = document.createElement("a");
      link.href = citation.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = `${citation.rank}. ${citation.title || citation.url}`;
      block.appendChild(link);
    }
    bubble.appendChild(block);
  }

  wrapper.append(avatar, bubble);
  return wrapper;
}

function makeTag(kind, text) {
  const span = document.createElement("span");
  span.className = `tag ${kind}`;
  span.textContent = text;
  return span;
}

function scheduleRender() {
  if (state.renderQueued) return;
  state.renderQueued = true;
  requestAnimationFrame(() => {
    state.renderQueued = false;
    renderMessages();
  });
}

function scrollToBottom() {
  const chat = $("chat");
  chat.scrollTop = chat.scrollHeight;
}

async function sendMessage(event) {
  event?.preventDefault();
  if (state.streaming) return;
  const input = $("message-input");
  const text = input.value.trim();
  if (!text && !state.attachment) return;

  let session;
  try {
    session = await ensureSession();
  } catch (error) {
    toast(error.message, true);
    return;
  }

  if (!state.session?.title || state.session.title === "New conversation") {
    const title = (text || "图片对话").slice(0, 60);
    api(`/v1/sessions/${session.id}`, { method: "PATCH", body: JSON.stringify({ title }) })
      .then((data) => { state.session = data.session; updateSessionHeader(); loadSessions(); })
      .catch(() => {});
  }

  const parts = [];
  if (text) parts.push({ modality: "text", content: text });
  if (state.attachment) {
    parts.push({
      modality: "image",
      content: state.attachment.dataUrl,
      metadata: {
        filename: state.attachment.name,
        mime_type: state.attachment.mime,
        alt: state.attachment.alt || "",
      },
    });
  }

  const userMessage = {
    id: crypto.randomUUID(),
    role: "user",
    content: text,
    parts,
    created_at: new Date().toISOString(),
    pending: false,
  };
  const assistant = {
    id: "pending",
    role: "assistant",
    content: "",
    parts: [],
    created_at: new Date().toISOString(),
    pending: true,
    tags: null,
    citations: [],
    error: null,
    model: state.currentModel,
  };
  state.messages.push(userMessage, assistant);
  renderMessages();
  input.value = "";
  autoResize();
  state.attachment = null;
  clearAttachment();

  const controller = new AbortController();
  state.controller = controller;
  setStreaming(true);

  const payload = {
    event_id: crypto.randomUUID(),
    session_id: session.id,
    parts,
    metadata: {
      model: state.currentModel,
      prompt_profile: state.currentPrompt,
      web_search: state.webSearch,
      locale: "zh-CN",
    },
  };

  let sawFinal = false;
  try {
    const response = await fetch("/v1/events", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok) {
      let data = null;
      try { data = await response.json(); } catch { /* ignore */ }
      throw new Error(data?.error?.message || `请求失败（${response.status}）`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseSseBlock(block);
        if (!parsed) continue;
        if (parsed.event === "meta") {
          state.session = { ...(state.session || {}), id: parsed.data.session_id, title: state.session?.title || "新对话" };
        } else if (parsed.event === "route") {
          assistant.model = parsed.data.model || assistant.model;
          assistant.fallback = parsed.data.fallback;
        } else if (parsed.event === "delta") {
          assistant.pending = true;
          assistant.content += parsed.data.content || "";
          assistant.tags = parsed.data.tags || assistant.tags;
          scheduleRender();
        } else if (parsed.event === "citations") {
          assistant.citations = parsed.data.citations || [];
        } else if (parsed.event === "final") {
          sawFinal = true;
          assistant.id = parsed.data.event_id;
          assistant.content = parsed.data.content;
          assistant.tags = parsed.data.tags;
          assistant.pending = false;
          assistant.error = null;
          scheduleRender();
        } else if (parsed.event === "warning") {
          toast(parsed.data.message || "输出中断，已保存部分内容。", true);
        } else if (parsed.event === "error") {
          const message = parsed.data?.error?.message || "模型请求失败";
          if (!assistant.content) assistant.error = message;
          else toast(message, true);
        }
      }
    }
    if (!sawFinal && !assistant.error) {
      assistant.pending = false;
      assistant.error = assistant.content ? "输出中断（部分内容已保存）" : "模型没有返回内容";
    }
  } catch (error) {
    assistant.pending = false;
    if (error.name === "AbortError") {
      if (!assistant.content) assistant.error = "已停止";
      else assistant.error = null;
    } else {
      assistant.error = error.message || "请求失败";
      toast(assistant.error, true);
    }
  } finally {
    assistant.pending = false;
    state.controller = null;
    setStreaming(false);
    renderMessages();
    loadSessions();
  }
}

function parseSseBlock(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}

function setStreaming(value) {
  state.streaming = value;
  $("send").disabled = value;
  $("stop").hidden = !value;
  $("message-input").disabled = value;
}

function stopStreaming() {
  if (state.controller) state.controller.abort();
}

/* ---------------- attachments ---------------- */
function clearAttachment() {
  state.attachment = null;
  $("attachment-preview").hidden = true;
  $("attachment-image").removeAttribute("src");
  $("image-input").value = "";
}

function attachImage(file) {
  if (!file) return;
  if (!file.type.startsWith("image/")) { toast("请选择图片文件", true); return; }
  if (file.size > 8 * 1024 * 1024) { toast("图片需小于 8 MB", true); return; }
  const reader = new FileReader();
  reader.onload = () => {
    state.attachment = { dataUrl: reader.result, name: file.name, mime: file.type, alt: "" };
    $("attachment-image").src = reader.result;
    $("attachment-preview").hidden = false;
    const model = activeModel();
    if (model && !model.input_modalities?.includes("image")) {
      toast("当前模型不支持图片，将自动路由或降级为文字占位。", false);
    }
  };
  reader.readAsDataURL(file);
}

/* ---------------- prompts ---------------- */
async function openPromptModal() {
  $("prompt-modal").hidden = false;
  await refreshPromptProfiles();
  const target = state.profiles.find((item) => item.id === state.currentPrompt) || state.profiles[0];
  if (target) await editPromptProfile(target.id);
}

async function refreshPromptProfiles() {
  const data = await api("/v1/prompts/profiles");
  state.profiles = data.profiles || [];
  renderPromptOptions();
  const container = $("prompt-profiles");
  container.innerHTML = "";
  for (const profile of state.profiles) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `prompt-profile${profile.id === state.editingProfile ? " active" : ""}`;
    const name = document.createElement("span");
    name.className = "p-name";
    name.textContent = profile.name;
    const meta = document.createElement("span");
    meta.className = "p-meta";
    meta.textContent = `v${profile.active_version}${profile.builtin ? " · 内置" : ""}`;
    button.append(name, meta);
    button.addEventListener("click", () => editPromptProfile(profile.id));
    container.appendChild(button);
  }
}

async function editPromptProfile(profileId) {
  const data = await api(`/v1/prompts/profiles/${profileId}`);
  const profile = data.profile;
  state.editingProfile = profile.id;
  $("prompt-name").value = profile.name;
  $("prompt-template").value = profile.system_template || "";
  $("prompt-name").disabled = Boolean(profile.builtin);
  $("delete-prompt").disabled = Boolean(profile.builtin);
  const versions = $("prompt-versions");
  versions.innerHTML = "";
  for (const version of profile.versions || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `version-pill${version.version === profile.active_version ? " active" : ""}`;
    button.textContent = `v${version.version}`;
    button.title = version.change_note || "";
    if (version.version !== profile.active_version) {
      button.addEventListener("click", async () => {
        try {
          await api(`/v1/prompts/profiles/${profile.id}/activate`, {
            method: "POST", body: JSON.stringify({ version: version.version }),
          });
          await refreshPromptProfiles();
          await editPromptProfile(profile.id);
          toast(`已激活 v${version.version}`);
        } catch (error) { toast(error.message, true); }
      });
    }
    versions.appendChild(button);
  }
  await refreshPromptProfiles();
}

function newPromptProfile() {
  state.editingProfile = null;
  $("prompt-name").disabled = false;
  $("prompt-name").value = "";
  $("prompt-template").value = "";
  $("delete-prompt").disabled = true;
  $("prompt-versions").innerHTML = "";
  document.querySelectorAll(".prompt-profile").forEach((item) => item.classList.remove("active"));
}

async function savePromptProfile() {
  const name = $("prompt-name").value.trim();
  const template = $("prompt-template").value;
  if (!name) { toast("请填写配置名称", true); return; }
  try {
    let data;
    if (state.editingProfile) {
      data = await api(`/v1/prompts/profiles/${state.editingProfile}`, {
        method: "PUT", body: JSON.stringify({ system_template: template, change_note: "edited from web UI" }),
      });
    } else {
      data = await api("/v1/prompts/profiles", {
        method: "POST", body: JSON.stringify({ name, description: "", system_template: template }),
      });
      state.editingProfile = data.profile.id;
    }
    await loadSettings();
    await refreshPromptProfiles();
    await editPromptProfile(data.profile.id);
    toast("已保存为新版本");
  } catch (error) { toast(error.message, true); }
}

async function resetPromptProfile() {
  if (!state.editingProfile) return;
  if (!confirm("重置会创建一个等于初始版本的新版本，历史版本保留。继续？")) return;
  try {
    await api(`/v1/prompts/profiles/${state.editingProfile}/reset`, { method: "POST" });
    await editPromptProfile(state.editingProfile);
    toast("已重置到初始版本");
  } catch (error) { toast(error.message, true); }
}

async function deletePromptProfile() {
  if (!state.editingProfile) return;
  if (!confirm("删除这个提示词配置？历史版本将一并删除，无法恢复。")) return;
  try {
    await api(`/v1/prompts/profiles/${state.editingProfile}`, { method: "DELETE" });
    state.editingProfile = null;
    await loadSettings();
    await refreshPromptProfiles();
    $("prompt-template").value = "";
    $("prompt-name").value = "";
    toast("已删除");
  } catch (error) { toast(error.message, true); }
}

/* ---------------- providers / health ---------------- */
async function openProviders() {
  $("providers-modal").hidden = false;
  try {
    const data = await api("/v1/providers");
    const list = $("provider-list");
    list.innerHTML = "";
    for (const provider of data.providers || []) {
      const card = document.createElement("div");
      card.className = "provider";
      const head = document.createElement("div");
      head.className = "p-head";
      const name = document.createElement("strong");
      name.textContent = provider.label;
      const badge = document.createElement("span");
      badge.className = `badge ${provider.configured ? "ok" : "off"}`;
      badge.textContent = provider.configured ? "已配置" : provider.requires_key ? "缺密钥" : "未启用";
      head.append(name, badge);
      const detail = document.createElement("div");
      detail.className = "p-detail";
      const env = provider.requires_key ? `环境变量：${provider.api_key_env || "—"}` : "本地 / 无需密钥";
      detail.textContent = `${provider.type} · ${provider.base_url || "local"}\n${env}`;
      card.append(head, detail);
      list.appendChild(card);
    }
  } catch (error) {
    toast(error.message, true);
  }
  try {
    const data = await api("/v1/memory/status");
    const memory = data.memory;
    $("memory-status").textContent = [
      `state: ${memory.state}`,
      `configured: ${memory.configured}`,
      `reason: ${memory.reason || "—"}`,
      `contract: ${memory.contract?.version || "1.0"} (Reserved)`,
      memory.base_url ? `base_url: ${memory.base_url}` : "base_url: —",
    ].join("\n");
  } catch { $("memory-status").textContent = "unavailable"; }
}

async function refreshHealth() {
  try {
    const data = await api("/v1/health");
    const configured = data.models?.configured || 0;
    const dot = $("status-dot");
    dot.className = `dot ${configured ? "ok" : "warn"}`;
    $("status-text").textContent = `${configured} 个模型可用 · 本地存档`;
    $("composer-hint").title = data.memory?.state === "disabled"
      ? "外部记忆扩展未接入（Reserved），当前使用本地存档与最近窗口。"
      : `记忆扩展：${data.memory?.state}`;
  } catch {
    $("status-dot").className = "dot bad";
    $("status-text").textContent = "服务不可达";
  }
}

/* ---------------- events ---------------- */
function autoResize() {
  const textarea = $("message-input");
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`;
}

function bindEvents() {
  $("composer").addEventListener("submit", sendMessage);
  $("message-input").addEventListener("input", autoResize);
  $("message-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); }
  });
  $("new-chat").addEventListener("click", newChat);
  $("stop").addEventListener("click", stopStreaming);
  $("attach-image").addEventListener("click", () => $("image-input").click());
  $("image-input").addEventListener("change", (event) => attachImage(event.target.files?.[0]));
  $("remove-attachment").addEventListener("click", clearAttachment);
  $("export-session").addEventListener("click", () => {
    if (!state.session) return;
    window.location.href = `/v1/sessions/${state.session.id}/export?format=json`;
  });
  $("delete-session").addEventListener("click", async () => {
    if (!state.session) return;
    if (!confirm("删除此对话及全部消息？此操作不可恢复。")) return;
    try {
      await api(`/v1/sessions/${state.session.id}`, { method: "DELETE" });
      newChat();
      await loadSessions();
      toast("对话已删除");
    } catch (error) { toast(error.message, true); }
  });
  $("model-select").addEventListener("change", async (event) => {
    state.currentModel = event.target.value;
    updateComposerHint();
    if (!state.session) return;
    try {
      const data = await api(`/v1/sessions/${state.session.id}/model`, {
        method: "POST", body: JSON.stringify({ model: state.currentModel }),
      });
      state.session = data.session;
      toast(`已切换到 ${activeModel()?.label || state.currentModel}，上下文保留`);
    } catch (error) { toast(error.message, true); }
  });
  $("prompt-select").addEventListener("change", async (event) => {
    state.currentPrompt = event.target.value;
    if (!state.session) return;
    try {
      const data = await api(`/v1/sessions/${state.session.id}/prompt`, {
        method: "POST", body: JSON.stringify({ profile_id: state.currentPrompt }),
      });
      state.session = data.session;
    } catch (error) { toast(error.message, true); }
  });
  $("search-toggle").addEventListener("change", async (event) => {
    state.webSearch = event.target.checked;
    if (!state.session) return;
    try {
      const data = await api(`/v1/sessions/${state.session.id}`, {
        method: "PATCH", body: JSON.stringify({ web_search: state.webSearch }),
      });
      state.session = data.session;
    } catch (error) { toast(error.message, true); }
  });
  $("open-prompts").addEventListener("click", openPromptModal);
  $("open-providers").addEventListener("click", openProviders);
  $("save-prompt").addEventListener("click", savePromptProfile);
  $("reset-prompt").addEventListener("click", resetPromptProfile);
  $("delete-prompt").addEventListener("click", deletePromptProfile);
  $("new-prompt-profile").addEventListener("click", newPromptProfile);
  $("toggle-sidebar").addEventListener("click", () => $("sidebar").classList.toggle("open"));
  document.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => { $(button.dataset.close).hidden = true; });
  });
  document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) backdrop.hidden = true;
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      document.querySelectorAll(".modal-backdrop").forEach((item) => { item.hidden = true; });
    }
  });
}

async function init() {
  bindEvents();
  try {
    await loadSettings();
    await loadSessions();
    await refreshHealth();
    updateSessionHeader();
    renderMessages();
    if (!state.models.some((item) => item.available)) {
      toast("尚未配置任何模型。打开「厂商与模型」或启用离线诊断模型。", true);
    }
  } catch (error) {
    $("status-dot").className = "dot bad";
    $("status-text").textContent = "初始化失败";
    toast(error.message, true);
  }
  setInterval(refreshHealth, 30000);
}

document.addEventListener("DOMContentLoaded", init);
