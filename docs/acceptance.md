# 验收对照

> 本文对应任务书 **v1.1** + 自定义提供方需求（v1.2）；外部记忆管线仍为 Reserved 扩展。

## 1. 功能需求

| # | 要求 | 实现 | 证据 |
| --- | --- | --- | --- |
| 1 | 多厂商/多模型接入与运行时切换，延续上下文 | 16 云厂商 + 4 本地；会话级切换；消息厂商无关 | `gateway/router.py`、`/v1/sessions/{id}/model`、`test_model_switch_uses_same_archive_context` |
| 2 | 切换故障自动降级兜底 | 同厂商重试 → 模型回退链 → 已配置模型重路由 | `test_fallback_preserves_context`、`test_provider_timeout_falls_back`、`test_offline_default_model_reroutes...` |
| 3 | 模型代理：转发、流式、超时/重试/限流 | SSE、连接/首 Token/读取超时、指数退避、令牌桶 | `gateway/router.py`、`test_local_rate_limit_falls_back`、`test_sse_event_contract` |
| 4 | 提示词模板/版本/变量/system prompt；与模型解耦 | PromptProfile + 不可变版本 + Jinja2 沙箱 | `prompts/manager.py`、`test_prompt_profile_version_lifecycle`、`test_prompt_is_decoupled...` |
| 5 | 预留提示词更换接口，供后期 UI | 读/建/存/激活/重置/删/预览 | `/v1/prompts/*`、`docs/prompt-api.md`、页面提示词管理 |
| 6 | 测试人格测试后删除 | 仓库默认系统提示词为空；临时测试配置仅在临时数据目录创建，验收前删除 | `test_prompts` 中无固定人设；发布检查；`docs/test-report.md` |
| 7 | 联网搜索（工具/RAG）、源可配置、引用溯源 | RAG；Tavily/Brave/SearXNG/DDG/自定义；引用表 + 正文来源 | `search/service.py`、`test_search_only_sends_current_query...` |
| 8 | 多模态能力协商/降级/路由 | 能力表、视觉路由、文本化降级、回退时剥离图片 | `test_capability_route_to_vision_model`、`test_capability_degrade...`、`test_vision_primary_falls_back...` |
| 9 | 对话存档、历史、裁剪、导出 | SQLite WAL；最近窗口预算；JSON/MD；删除 | `sessions/service.py`、`test_archive_create_export_delete`、`test_context_builder_keeps_latest_and_trims` |
| 10 | 情感/类型/意图标签随输出事件 | 本地确定性分类；delta/final 都带 tags | `classifier.py`、`test_sse_event_contract`、`test_classifier.py` |
| 11 | 厂商推荐清单（BYOK） | 16 + 4 清单、Base URL、env | `docs/providers.md`、`/v1/providers` |
| 12 | 开发文档 + 用户文档 | 架构/API/提示词/厂商/降级/记忆/隐私/测试/用户/开发 | `docs/` |

## 2. 自定义模型提供方（v1.2）

| 要求 | 实现 | 证据 |
| --- | --- | --- |
| 新增自定义提供方（名称/Base URL/Key/模型/协议/头） | 设置页表单 + `/v1/custom-providers` | `test_custom_provider_crud...`、Edge CDP UI E2E |
| 模型手动或 `/models` 拉取 | 手动行 + discover API/UI；>200 截断并提示 | `test_custom_provider_discovery...`、`test_model_limit_is_readable_at_boundary` |
| 列表/编辑/删除/启停 | CRUD + enabled 接口 | 同上、`test_disable_default...` |
| 默认提供方/模型 | `app_settings` + `/v1/provider-defaults`；新建会话持久化该设置 | `test_new_session_uses_provider_defaults...`、UI E2E |
| 保存前测试，五类结果 | 三级测试 + 状态映射；无 Key 回显 | `test_custom_provider_test_levels` |
| 严格 Base URL，不重复 `/v1` | `providers/urls.py`；纯域名前端非强制提示 | `test_strict_url_join...` |
| 流式/非流式聊天 | 动态 OpenAI-compatible 适配器 | `test_custom_chat_stream...`、UI E2E |
| Key 加密/掩码/不导出 | AES-GCM + `apiKeyMasked` + export 脱敏 | `test_custom_provider_crud...`、`test_export_import...` |
| 删除正在使用：确认 + 默认切换 | 409 `DEFAULT_PROVIDER_REQUIRED` / `PROVIDER_IN_USE` | `test_delete_default_in_use...` |
| 历史保留、标记不可用、手动迁移 | `providerAvailable` + 迁移 API/向导 | 同上 |
| 能力为一等字段 + 预设 | `capabilities` + `CAPABILITY_PRESETS` + UI | `test_custom_capability...` |
| 多模态探测/未验证 | 可选 1×1 图片探测；verification 字段 | `test_custom_provider_discovery...` |
| 导入导出不含 Key | 导出 JSON 无 Key；导入禁用以待填 | `test_export_import...` |
| 非 OpenAI 协议明确拒绝 | `UNSUPPORTED_PROTOCOL` + 单选项 UI | `test_provider_validation_errors` |

## 3. 外部记忆管线（v1.1）

| 要求 | 实现 | 证据 |
| --- | --- | --- |
| 原协作取消，不在本期范围 | 文档与 README 明确 | `README.md`、`docs/memory-reserved.md` |
| 保留第四节查询 JSON/健康/写回契约 | v1.0 冻结；Pydantic 模型 + 常量 | `memory/contract.py` |
| 插件/适配器，可启用/禁用，默认禁用 | `MemoryPlugin` / `Disabled` / `Http` | `memory/plugin.py`、`config/default.yaml` |
| 未接入时健康探测固定 unavailable | 禁用插件返回 `state=unavailable` | `test_disabled_extension_has_zero_impact...` |
| 主流程零影响 | 禁用零 I/O；启用失败仅回退 | 同上、`test_memory_timeout...`、`test_memory_query_failure...` |
| 常规方案为正式路径 | 存档 + 最近窗口按正式功能测试 | `test_archive...`、`test_context...` |
| 可被未来扩展直接调用，mock 验证 | `HttpMemoryPlugin.health/query/writeback` | `test_http_plugin_health_and_query_can_be_called_directly` |
| 不得删除接口 | 文档/测试/代码保留 | 发布检查 + `docs/memory-reserved.md` |

## 4. 隐私与安全

| 要求 | 实现 | 证据 |
| --- | --- | --- |
| 对话本地存储，不上传网络 | SQLite 默认 `data/`；无遥测 | `storage/database.py`、`privacy-security.md` |
| 密钥仅本地、不代管、不写日志 | 内置厂商用环境变量；自定义 Key AES-GCM 加密，仅返回掩码 | `test_custom_provider_crud...`、`test_custom_key_never...`、`test_redactor...` |
| 存档查看/导出/删除 | API + 页面 | `test_archive_create_export_delete` |
| 搜索仅发用户配置的请求，不携带无关对话 | 只发 `query`/`top_k` | `test_search_only_sends_current_query...` |
| 不提交 `.env`/对话/密钥/日志 | `.gitignore` + 发布清单 | 仓库状态检查 |
| 日志不输出敏感内容 | 白名单字段 + filters | `logging.py`、测试 |

## 5. 可独立运行

| 检查 | 结果 |
| --- | --- |
| 仅依赖 PyPI 包，不依赖上下游模块 | 是 |
| 输入事件 → 网关 → 带标签输出 | 是（真实厂商或 echo） |
| 零密钥可演示 | `OMNI_ENABLE_ECHO=1` |
| 一键启动 | `python -m omninexus` |
| 页面 | `/` 无构建；API 文档 `/docs` |
| 容器 | `docker compose up --build` |
| 测试不依赖外部网络/密钥 | 全部 `httpx.MockTransport` / echo |

## 6. 自评估与故障注入

| 项 | 状态 |
| --- | --- |
| 多模型切换 | 通过（自动化） |
| 能力协商/降级 | 通过（自动化） |
| 故障回退/重试/限流/超时 | 通过（自动化） |
| 记忆插件禁用/超时/500/熔断 | 通过（自动化） |
| 搜索失败 | 通过（自动化） |
| 存档导出/删除/裁剪 | 通过（自动化） |
| SSE 契约 | 通过（自动化 + 本地真实服务器） |
| Emotion contract v2 / 旧标签归一 | 通过（自动化） |
| session 顶层/兼容回退/创建信号 | 通过（自动化） |
| 引用默认不进入正文、citations 仍可用 | 通过（自动化） |
| 单厂商首跑默认回退 | 通过（自动化） |
| 跨仓库契约端点/版本语义 | 通过（自动化） |
| 旧会话兼容：空会话迁移、有回答保持 + 可读 409 | 通过（自动化） |
| 错误体 `retryable`/`request_id` 齐备 | 通过（自动化） |
| 根因分析 | 见 `docs/test-report.md` |
| 真实厂商在线调用 | 未在 CI 执行（无密钥）；适配器用 mock 校验，用户填 Key 后使用 |

## 7. 提交规范检查

- [x] 删除测试数据/日志/临时数据库/截图（发布前执行，见 `docs/development.md` 清单）
- [x] 无测试人格/正式人设内容
- [x] 无 `.env`，仅有 `.env.example`
- [x] 无真实密钥
- [x] 日志框架脱敏，日志文件 Git 忽略
- [x] 仓库可独立安装运行
- [ ] 本次 0.3.0 修复按用户要求仅保留在本地，不推送 GitHub
- [x] 历史交付曾推送到 `https://github.com/zhipianxiaokonglong/OmniNexus`

> 最终提交前应以 `git status`、`git ls-files` 与测试报告为准复核本表。
