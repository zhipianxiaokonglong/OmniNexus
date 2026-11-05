# Changelog

## 0.3.1 — 2026-11-05

### Fixed

- **默认提供方/模型未作用于新会话**：`orchestrator` 在创建会话前读取
  `/v1/provider-defaults`，并把 `provider_kind/provider_id/model_name` 与 canonical
  `model` 一起持久化；不再被静态 `gateway.default_model` 遮蔽（Dounai 审计）。
- 会话 `model` 与拆分字段统一生成，避免新旧字段不一致。
- 历史会话保持原样、不静默迁移；不可用提供方仍返回 `409 PROVIDER_UNAVAILABLE`。
- 新增回归测试 `test_new_session_uses_provider_defaults_not_gateway_default`。

## 0.3.0 — 2026-11-03 / hotfix 2026-11-05

### Changed

- **Emotion contract v2**：对外只输出 12 类（neutral/joy/excitement/comfort/tenderness/calm/
  sadness/apology/encouragement/surprise/frustration/anger）；旧 `fear→sadness`、
  `confusion→frustration`、`gratitude→joy` 在边界归一。
- `GET /v1/health`、`GET /v1/settings` 增加 `tag_contract`；SSE/JSON 增加
  `X-OmniNexus-Tag-Contract: 2`。
- `metadata.session_id` 兼容回退 + warning；顶层 `session_id` 缺失时 warning 并在
  `meta`/响应头标记 `session_created`。
- `gateway.citations_in_content` 默认 `false`：来源 URL 不再进入 `final.content`，仍通过
  citations 事件与 API 提供。
- 默认模型不可用时优先选择默认厂商的已配置模型，减少“只配一家厂商首跑失败”。
- **修复设置页默认提供方/模型不生效**：新建会话直接持久化 `provider-defaults`，不再被
  `gateway.default_model` 遮蔽；历史会话保持原样、不静默迁移（Dounai 审计 2026-11-05）。
- 版本与 tag `v0.3.0`；新增 `docs/COMPATIBILITY.md` 与发布说明。

## 0.2.0 — 2026-10-30

### Added

- Chatbox 式自定义模型提供方：名称、Base URL、API Key、模型列表、能力、请求头、协议、超时。
- 提供方列表/编辑/删除/启用禁用；默认提供方与默认模型；导入/导出（不含 Key）。
- 三级连接测试（地址、`/models`、最小聊天）+ 可选多模态探测 + 未验证状态。
- API Key 本机 AES-GCM 加密、前端仅掩码、日志/错误/导出不含 Key。
- 自定义模型能力字段与预设，图片输入同提供方内视觉路由/文本降级。
- 历史会话不可用标记 + 手动迁移向导；删除默认/占用提供方保护。
- 系统设置页（`/settings`）与聊天页“提供方 + 模型”双选择器。

### Changed

- 内置厂商保持只读；自定义提供方仅支持 OpenAI-compatible。
- Base URL 严格拼接，不自动补 `/v1`；纯域名仅前端非强制提示。
- 数据库 schema v2（自定义提供方/模型、默认设置、会话 provider 字段）。

## 0.1.0 — 2026-10-24

首个可独立运行的交付版本（任务书 v1.1）。

### Added

- 多厂商模型网关：OpenAI-compatible（OpenAI、DeepSeek、Mistral、xAI、Qwen、Doubao、GLM、
  Moonshot、Qianfan、Spark、Hunyuan、MiniMax、OpenRouter、Ollama、LM Studio、vLLM、llama.cpp）、
  Anthropic、Gemini、Cohere 原生适配器。
- 运行时模型切换、上下文延续、重试、首 Token 超时、进程内限流、模型回退链与已配置模型重路由。
- 多模态能力协商：视觉路由、文本化降级、回退时图片剥离。
- 提示词档案与不可变版本：创建/保存/激活/重置/删除/预览；Jinja2 沙箱变量注入。
- 本地对话存档（SQLite WAL）：会话、消息、引用、导出 JSON/Markdown、删除、Token 预算裁剪。
- 确定性情感/内容类型/意图标签，随 `delta`/`final` 事件下发。
- 可配置联网搜索（Tavily/Brave/SearXNG/DuckDuckGo/自定义），RAG + 引用溯源，查询最小化。
- SSE 契约：`meta`/`route`/`delta`/`final`/`citations`/`warning`/`error` 与心跳。
- 本地零构建 Web 页面：对话、模型/提示词切换、图片、搜索、存档、导出/删除、提示词管理。
- 隐私：密钥仅环境变量、日志脱敏、错误正文不回显、`.env`/data/log 忽略。
- **Reserved**：外部记忆插件接口（默认禁用、健康探测 `unavailable`、契约 v1.0、mock 验证）。
- 文档：架构、API、提示词、厂商、降级、隐私、用户、开发、测试报告、验收对照。
- CI、Dockerfile、docker-compose、MIT License。

### Notes

- 未内置正式人设或测试人格；默认系统提示词为空。
- 外部记忆管线不参与本期交付。
- 不提供密钥/账户（BYOK）。
