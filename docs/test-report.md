# 测试与自评估报告

- 版本：OmniNexus 0.2.0（自定义模型提供方）
- 任务书：v1.1 + 自定义提供方需求；外部记忆管线仍为 Reserved
- 测试日期：2026-10-30
- 环境：Windows 11（amd64），Python 3.13.9，pytest 8.x，httpx MockTransport，SQLite 3
- 真实付费厂商：**未调用**（无密钥，CI 也不调用）
- 测试数据：临时目录，运行后删除；仓库不含对话/日志/数据库/测试人格

## 1. 结果摘要

```text
73 passed
ruff check: All checks passed
coverage: 81% statements/branches (见 §9)
```

覆盖重点：

- 多模型切换与上下文延续；
- 首 Token 前故障的重试、回退、超时、限流；
- 多模态能力路由与降级；
- SSE/JSON 冻结事件契约；
- 提示词版本/激活/回滚/重置/沙箱；
- 存档创建、读取、导出、删除、窗口裁剪；
- 搜索查询最小化与引用；
- 密钥脱敏与错误正文不回显；
- 保留记忆插件默认禁用、超时/500/熔断零影响、mock 直接调用。

## 2. 测试分层

| 文件 | 关注点 |
| --- | --- |
| `test_classifier.py` | 情感/类型/意图标签、输出 shape |
| `test_prompts.py` | 配置/版本/激活/重置/删除/内置保护 |
| `test_prompt_integration.py` | 变量注入 system message、模型切换后提示词不变 |
| `test_archive.py` | 存档、导出、删除、标题、上下文裁剪 |
| `test_gateway.py` | 重试/回退/超时/限流、切换、能力路由/降级、全失败 |
| `test_contract.py` | SSE 帧、JSON 精确字段、幂等、输入兼容、校验 |
| `test_search.py` | 只发当前查询、引用、搜索失败不阻断、默认禁用 |
| `test_search_providers.py` | Tavily/Brave/SearXNG/DuckDuckGo 请求形状与解析 |
| `test_memory_reserved.py` | 禁用零影响、健康/查询/写回、超时、熔断、直接调用 |
| `test_provider_adapters.py` | Anthropic/Gemini/Cohere 原生 SSE + 图片编码 + 401 |
| `test_custom_providers.py` | 自定义提供方 CRUD、Key 加密/掩码、三级测试、多模态探测、严格 URL、默认/删除/禁用、导入导出、迁移、日志脱敏 |
| `test_privacy.py` | 脱敏、metadata 清理、密钥不出 API、上游正文不回显、运行日志不含对话 |
| `test_config.py` | 默认禁用、离线 echo、默认配置打包一致、无密钥序列化 |

## 3. 多模型 / 能力协商 / 降级

| 用例 | 断言 | 结果 |
| --- | --- | --- |
| `test_model_switch_uses_same_archive_context` | 切到 beta 后请求包含 alpha 的历史助手消息 | PASS |
| `test_fallback_preserves_context` | alpha 失败后 beta 收到同一上下文并回答 | PASS |
| `test_provider_timeout_falls_back` | alpha 读超时 → beta 成功 | PASS |
| `test_local_rate_limit_falls_back` | 本地令牌桶耗尽 → beta 成功 | PASS |
| `test_offline_default_model_reroutes_to_configured_echo` | 请求链全未配置 → 已配置 echo 兜底 | PASS |
| `test_capability_route_to_vision_model` | 图片输入路由到视觉模型，payload 含 image_url | PASS |
| `test_capability_degrade_to_text_when_no_vision` | 无视觉候选 → 文本占位，不发送 image_url | PASS |
| `test_vision_primary_falls_back_to_text_safely` | 视觉主模型失败 → 文本回退，图片被剥离 | PASS |
| `test_retry_same_provider_before_fallback` | 同厂商失败一次后重试成功，不调用备用 | PASS |
| `test_all_providers_failed_returns_structured_error` | 503 + `ALL_PROVIDERS_FAILED` + attempts | PASS |
| `test_sse_event_contract` | meta/route/delta/final，delta stream=true，final=false | PASS |

## 4. 故障注入矩阵

| 注入 | 方法 | 期望 | 实际 |
| --- | --- | --- | --- |
| 主厂商 500 | MockTransport 500 | 回退到下一候选 | PASS |
| 主厂商读超时 | 抛 `httpx.ReadTimeout` | 回退 | PASS |
| 厂商 401 | MockTransport 401 | 不重试、换候选；全部失败结构化错误 | PASS（adapter 单测） |
| 本地限流 | `rpm=1`, `max_wait=0` | 429 → 回退 | PASS |
| 空内容 | 无文本 token | 重试/回退 | PASS（逻辑覆盖） |
| 记忆禁用 | `enabled=false` | 零 I/O，回答正常，状态 unavailable | PASS |
| 记忆健康 500 | Mock 503 | 跳过，回答正常 | PASS |
| 记忆查询 500 | Mock 500 | 回退最近窗口 | PASS |
| 记忆超时 | 抛 ReadTimeout | 回退，不阻断 | PASS |
| 记忆熔断 | 连续失败 ≥2 | circuit_open，停止探测 | PASS |
| 搜索 503 | Mock 503 | 回答正常、无引用 | PASS |
| 提示词未定义变量 | 模板 `{{ missing }}` | 空值渲染，对话成功 | PASS |
| 非法提示词 | `{% for %}` | 422 PROMPT_INVALID | PASS |
| 非法输入 UUID | Pydantic | 422，不回显输入 | PASS |
| 未知模型 | `metadata.model` | 404 UNKNOWN_MODEL | PASS |
| 重复投递 | 同 event_id | 幂等，不重复调用模型 | PASS |
| 中文导出标题 | Content-Disposition | 200，RFC 5987 编码 | PASS |

## 5. 隐私验证

| 检查 | 结果 |
| --- | --- |
| `/v1/providers` 不返回密钥值 | PASS |
| `/v1/custom-providers` 只返回掩码，DB 为 AES-GCM 密文 | PASS |
| 自定义 Key 不出现在日志/测试错误/导出 | PASS |
| `/v1/settings` 不返回密钥 | PASS |
| 上游 500 正文含密钥时不回显 | PASS |
| 日志脱敏常见密钥格式 + 已加载密钥 | PASS |
| 输入 parts metadata 中 api_key/authorization 被丢弃 | PASS |
| 搜索请求体只有 query/top_k，无历史/会话 ID | PASS |
| 对话原文不出现在错误响应 | PASS |
| `.env`/data/log 在 `.gitignore` | 仓库检查 |

## 6. 保留记忆接口（v1.1）

| 验证 | 结果 |
| --- | --- |
| 默认 `memory.enabled=false` | PASS |
| 禁用时 `health` 固定 `unavailable` | PASS |
| 禁用时无任何 memory HTTP 请求 | PASS |
| 启用后 mock 查询条目注入 `<external_memory>` | PASS |
| 查询 JSON 字段严格冻结 | PASS |
| 健康头 `X-OmniNexus-Memory-Contract: 1.0` | PASS |
| 写回 payload 含 user/assistant | PASS |
| 查询 500/超时/熔断不影响回答 | PASS |
| 插件可直接被未来扩展调用 | PASS |

## 7. 真实运行与 UI 检查

在 `127.0.0.1:8877` 启动真实 uvicorn（`OMNI_ENABLE_ECHO=1`，临时数据目录）：

| 检查 | 结果 |
| --- | --- |
| `GET /` | 200，CSP/安全头正常 |
| `GET /static/app.js` | 200 |
| `POST /v1/events` SSE | 200，52 帧，首事件 37ms，final 正常 |
| Edge headless DOM | 会话列表、39 个模型选项、状态“1 个模型可用”渲染成功 |
| Edge CDP 实际发送 | 2 条消息、标签“中性/文本/回复”、无 JS 异常 |
| 自定义提供方 UI（Edge CDP） | 添加表单、Base URL 补 `/v1` 提示、三级测试成功、保存、设默认、聊天流式回复，全部通过 |
| Key 展示 | 卡片显示 `sk-****1234`，无完整 Key |
| 页面截图 | 无遮挡、无空白模态框（修复 `[hidden]` 后） |

浏览器自动化不是 CI 的一部分（本机 Edge 驱动不稳定）；CI 覆盖 API/SSE 契约。

## 8. 根因分析（从缺陷到机制）

### R-01 存档读取 `no such column: rowid`（P1）

- **现象**：真实服务首次落地后，`GET /v1/sessions/{id}` 500；单测此前未覆盖读取路径。
- **直接原因**：`list_messages` 使用 `SELECT * FROM (SELECT * ... ORDER BY rowid DESC)`，派生表
  不暴露 `rowid`，外层再按 `rowid` 排序触发 SQLite 错误。
- **根因**：分页查询把“取最新 N 条”与“按时间正序返回”组合时，未把排序键投影到派生表。
- **修复**：内层 `SELECT m.*, m.rowid AS _row_order`，外层按 `created_at, _row_order` 排序。
- **回归**：`test_archive_create_export_delete`、幂等测试读取会话详情。
- **预防**：真实 HTTP 冒烟纳入测试报告；新增 SQL 必须用真实 SQLite 执行，不 mock DB。

### R-02 中文标题导出 500（P1）

- **现象**：`GET /export?format=md` 对中文标题抛 `UnicodeEncodeError`。
- **直接原因**：`Content-Disposition` 直接放中文文件名，Starlette 用 latin-1 编码响应头。
- **根因**：HTTP header 字符集假设错误；只测了 ASCII 标题。
- **修复**：ASCII fallback + RFC 5987 `filename*=UTF-8''…`。
- **回归**：`test_archive_create_export_delete` 断言 `filename*=UTF-8''`。
- **预防**：所有用户可控 header 值使用编码函数；测试覆盖非 ASCII。

### R-03 请求模型未配置时 503，即使存在可用兜底（P1）

- **现象**：只启用 echo 时，未显式指定 model 的请求仍 503。
- **直接原因**：候选链只沿“请求模型 → 其 fallbacks”构建；echo 不在链中，虽然 provider 已配置。
- **根因**：把“模型回退”与“全局兜底”混为一谈，缺少“整条链无可用 provider”时的重路由分支。
- **修复**：链全不可用时，从所有已配置模型中选择首个，标记 `requested_unavailable`；本地 provider
  默认禁用，避免无意义的连接超时。
- **回归**：`test_offline_default_model_reroutes_to_configured_echo`、`test_config`。
- **预防**：故障矩阵明确“请求链不可用”和“厂商调用失败”两类；分别测试。

### R-04 上下文截断标记错误（P2）

- **现象**：`truncated_latest` 永远为 false；此前通过与已变异对象比较判断。
- **直接原因**：`messages[-1].content` 被原地截断后再与自身比较。
- **根因**：在可变对象上做“前后快照”比较，未先保存原值。
- **修复**：进入截断前保存 `original_latest`；测试断言截断标记与省略号。
- **回归**：`test_context_builder_truncates_oversized_latest_turn`。
- **预防**：对“是否修改”这类判定使用值快照或显式标志。

### R-05 页面模态框初始可见（P2）

- **现象**：首次截图两个模态框同时显示，遮挡聊天页。
- **直接原因**：作者样式 `.modal-backdrop { display: grid }` 覆盖了浏览器对 `[hidden]` 的默认
  `display:none`。
- **根因**：依赖 UA 样式，未显式定义 `[hidden]`。
- **修复**：全局 `[hidden] { display:none !important; }`。
- **回归**：headless 截图 + DOM 检查；后续可加视觉回归。
- **预防**：组件显隐用显式 class 或统一 `[hidden]` 规则。

### R-07 导入配置 422：导出文件根字段被拒绝（P2）

- **现象**：导出 JSON 直接导入返回 422。
- **直接原因**：`ImportPayload` 使用 `extra="forbid"`，导出文件含 `exportedAt` 元数据。
- **修复**：导入 schema 允许未知元数据（仍只读取 `schemaVersion`/`providers`）。
- **回归**：`test_export_import_never_contains_key`。

### R-08 自定义提供方默认模型丢失（P2）

- **现象**：编辑/保存后默认模型可能回落为第一个模型。
- **直接原因**：数据表初期未存 `default_model`，读取时用首个模型推断。
- **修复**：增加 `default_model` 列并在创建/更新时持久化；表单增加默认模型选择。
- **回归**：CRUD、默认设置与 UI E2E 检查。
- **预防**：前端配置对象字段必须在 DB/API 双向映射测试中覆盖。

### R-06 保留插件禁用状态返回 `disabled` 而非 `unavailable`（P2）

- **现象**：v1.1 要求“健康探测固定返回 unavailable”，实现返回自定义 `disabled`。
- **根因**：把内部原因与 wire 状态混用，未按补丁要求收敛状态词。
- **修复**：禁用/未配置统一 `state=unavailable`，原因放 `reason`。
- **回归**：`test_disabled_extension_has_zero_impact...`。
- **预防**：契约中的状态枚举由测试显式断言。

## 9. 覆盖率

```text
command: python -m pytest --cov=omninexus --cov-report=term -q
TOTAL 4336 stmts, 648 miss, 1232 branches, 324 partial, 81% coverage
```

主要模块：orchestrator 80%、gateway/router 79%、prompts 82%、sessions 83%、storage 91%、
search 81%、memory/plugin 80%、providers（store/service/tester 等）纳入新增测试、
OpenAI-compatible 79%、Anthropic 83%、Gemini 75%、Cohere 66%。

未覆盖部分主要是：真实厂商非 200 分支、浏览器交互、Docker 运行、保留记忆插件的极端网络错误。
核心路径（输入、网关流、回退、提示词、存档、搜索、标签、隐私）均有直接测试。CI 不设硬性覆盖率
阈值，以免用低价值测试堆数字。

## 10. 限制与未执行项

1. **未调用真实付费 API**：无密钥。OpenAI-compatible 用 MockTransport 模拟真实 SSE；Anthropic/
   Gemini/Cohere 原生适配器用 mock SSE + 图片编码测试。用户填入 Key 后即可实测。
2. **模型 ID/能力可能漂移**：默认表是工程基线，不作为厂商承诺；可在 YAML 修正。
3. **流式中断**：首 Token 前可透明回退；已输出后只保留部分并警告，不做跨模型拼接。这是有意的
   数据完整性选择。
4. **保留记忆仅为 mock**：本期无真实管线；接口可直接调用，但未与真实服务联调。
5. **单进程限流/熔断**：多 worker 部署需外部限流；SQLite 适合本地单用户/小团队。
6. **未做压力/长稳测试**：建议上线前补 100+ 并发与 24h 稳定性测试。
7. **无内建认证**：默认回环监听；对外必须加反向代理认证。
8. **自定义提供方不做跨提供方回退**：这是有意的数据边界；失败给可读错误。
9. **Web Key 加密依赖本机主密钥**：能访问本机账户的人仍可能读取；系统钥匙串留待桌面版。

## 11. 测试人格与仓库卫生

测试期在**系统临时目录**创建过一个临时提示词配置（测试人格标记），用于验证提示词 API 的创建/
激活/删除链路；验证后已通过 API 删除配置、删除临时 SQLite 与目录。仓库、文档、日志、导出和
提交历史中**不包含**该人格内容。发布前再次执行：

```powershell
git grep -n -I -E "persona|测试人格|test persona" -- . ':!docs/test-report.md'
Get-ChildItem -Recurse -Force -Include *.db,*.db-wal,*.db-shm,*.log,*.jsonl,*.png
```

结果应为空（本报告自身提及“测试人格”一词除外）。

## 12. 复现

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check src tests
```

离线手工验证：

```powershell
$env:OMNI_ENABLE_ECHO = "1"
$env:OMNI_DATA_DIR = "$env:TEMP\omninexus-smoke"
python -m omninexus --port 8877
```

测试结束后删除 `%TEMP%\omninexus-smoke`。
