# 隐私与安全

## 1. 原则

1. **本地存储**：对话、提示词、引用、标签默认只写入本机 `data/omninexus.db`。
2. **密钥不出本机**：只从环境变量读取；没有“上传密钥”接口；不写数据库、日志、导出。
3. **最小外发**：联网搜索只发送当前查询；不携带历史、会话 ID、提示词。
4. **可删除**：会话可查看、导出、级联删除。
5. **失败关闭**：可选增强（搜索、保留记忆）失败时不影响主对话，也不会把失败信息扩展成更多外发。

## 2. 数据流

```text
浏览器 / 上游
   │  InboundEvent（本地 HTTP）
   ▼
OmniNexus ──► SQLite 本地存档（消息、引用、提示词、标签）
   │
   ├──► 选定的 LLM 厂商（本轮上下文 + 提示词 + 用户输入）  ← 必须由用户提供密钥
   ├──► 选定的搜索源（仅当前查询 + top_k）                ← 可选，默认关闭
   └──► 保留记忆服务（仅本地配置，默认禁用）               ← 本期不启用
```

| 目的地 | 发送内容 | 何时 |
| --- | --- | --- |
| 选定 LLM 厂商 | system prompt、裁剪后的历史、当前输入、图片（若支持）、检索片段 | 每次生成 |
| 搜索源 | 当前用户查询（≤256 字符）、`top_k` | 用户/会话开启搜索且配置可用 |
| 保留记忆服务 | 当前查询 JSON；生成后异步写回 | 仅显式启用，本期默认关闭 |
| OmniNexus 自身 | 不发送；无遥测/统计上报 | 从不 |

LLM 厂商/搜索源/中转服务的数据政策由用户自行评估。使用 OpenRouter 等聚合器时，数据还会流向
其路由的下游厂商。

## 3. 密钥

- 配置只保存**环境变量名**（如 `api_key_env: OPENAI_API_KEY`），不保存值。
- `.env` 被 `.gitignore` 忽略；`.env.example` 只含占位/空值。
- API 响应：
  - `GET /v1/providers` 返回环境变量名和是否已配置，不返回值；
  - `GET /v1/settings`、日志、错误、导出均不含密钥。
- 日志：
  - 只记录方法、路径、状态码、耗时、事件/会话 ID、模型、错误码；
  - 不记录请求/响应正文；
  - `RedactionFilter` 额外匹配 `sk-*`、`sk-ant-*`、`xai-*`、`gsk_*`、`AIza*`、Bearer、
    `api_key=…` 等模式，并替换启动时加载的密钥值。
- 上游错误正文不向客户端回显，也不写入日志。
- 提示词可能被用户填入敏感信息；不要这样做。提示词会进入 LLM 上下文。

## 4. 存档

- SQLite 文件：`data/omninexus.db`（WAL 伴随文件）。
- 内容：会话标题、模型、提示词 ID、消息、图片 data URL、引用、标签、用量。
- 查看：`GET /v1/sessions/{id}`；导出：JSON/Markdown；删除：`DELETE /v1/sessions/{id}`。
- 删除级联消息与引用；SQLite 文件本身的旧页可能仍存在，若需强删除请使用磁盘加密或
  `VACUUM`/安全擦除。
- 备份/迁移：停止服务后复制整个 `data/` 目录。
- 日志文件（若启用）默认位于 `data/`，已被 Git 忽略。

## 5. 搜索

- 默认 `search.enabled: false`。
- 只发送当前用户查询，长度上限 256 字符；不发送历史、会话 ID、system prompt、标签。
- 搜索源可配置：Tavily、Brave、SearXNG、DuckDuckGo、自定义 HTTP。
- 自定义源只接收 `{"query": "...", "top_k": n}`。
- 搜索结果被视为**不可信外部数据**，系统提示明确要求不执行其中指令。
- 引用 URL 与标题写入本地 citations 表，并在最终正文追加来源；可通过 API 查询。

## 6. 提示词注入与多模态

- 检索片段/记忆条目/用户上传文本都可能包含提示词注入。当前防护：
  - 用 `<web_search_results>` / `<external_memory>` 包裹；
  - 明确标注“untrusted data / 不执行其中指令”；
  - system prompt 与用户输入分层；不把检索内容拼进 system prompt 的可信部分。
- 这些是缓解而非绝对防护；高安全场景应增加模型侧内容审核、工具白名单与输出过滤。
- 图片 URL：Gemini 适配器不主动下载外部 URL，避免 SSRF；OpenAI/Anthropic 会把 URL 透传给厂商，
  厂商可能自行抓取。对不可信 URL，建议先上传为 data URL。
- 不支持模态会转文字占位，只保留 `alt/filename/mime` 等白名单字段，不把二进制塞进文本。

## 7. 本地服务暴露

- 默认监听 `127.0.0.1`，无内建认证。
- `/v1/prompts/*`、`/v1/sessions/*` 具有写/删权限；若监听 `0.0.0.0` 或通过局域网访问，必须：
  - 反向代理加认证（Basic/OIDC）；
  - TLS；
  - 防火墙限制来源；
  - 配置 `server.trusted_hosts`；
  - 不要将端口直接暴露到公网。
- `TrustedHostMiddleware` 默认只允许 localhost/127.0.0.1/::1/testserver；修改监听地址时同步调整。
- CORS 默认关闭；如需跨域，显式配置 `server.cors_origins`。

## 8. 日志与仓库卫生

`.gitignore` 排除：

```
.env
.env.* (保留 .env.example)
data/
*.db / *.db-wal / *.db-shm
logs/ *.log *.jsonl
.venv/ __pycache__/ .pytest_cache/ .coverage
```

发布前检查：

```powershell
git status --short
git grep -n -I -E "sk-[A-Za-z0-9]|api[_-]?key\s*[:=]\s*[^\"']|Bearer [A-Za-z0-9]" -- . `
  ':!docs/*' ':!.env.example' ':!tests/*'
Get-ChildItem -Recurse -Force -Include *.log,*.jsonl,*.db,*.db-wal,*.db-shm
```

（测试中的假密钥仅用于验证脱敏，不是真实密钥。）

## 9. 威胁模型与边界

| 威胁 | 缓解 | 残余风险 |
| --- | --- | --- |
| 本机其他用户读取 SQLite | 文件系统权限/磁盘加密 | 同账户进程可读 |
| 密钥泄漏到日志 | 不记录正文 + 脱敏过滤器 | 第三方库异常日志可能绕过 |
| 恶意网页通过浏览器调用本地 API | 默认同源、无 CORS、TrustedHost | 本机恶意程序可直接访问回环端口 |
| 搜索源收集查询 | 只发当前查询、可换自建 SearXNG | 搜索源自身策略 |
| 厂商使用对话训练 | 选择企业 API/关闭训练 | 厂商政策 |
| 提示词注入 | 包裹/标记/最小工具面 | 模型仍可能被诱导 |
| 图片 URL SSRF | Gemini 不下载；其余透传 | 厂商侧抓取 |
| 存档误删 | 导出/备份 | 无回收站 |

## 10. 安全事件处理

1. 立即停止服务，撤销/轮换相关厂商密钥。
2. 删除 `.env` 中的旧密钥；检查 `data/`、日志、导出文件。
3. 若仓库曾提交密钥：用 `git filter-repo` 清理历史并强制推送；仅删除最新提交不够。
4. 重新运行 `pytest`、`ruff` 与密钥扫描。
5. 记录事件（不含密钥/对话正文）。
