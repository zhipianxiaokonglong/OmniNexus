# API 契约

Base URL：`http://127.0.0.1:8000`。交互式 OpenAPI：`/docs`，原始 schema：`/openapi.json`。

> **本地服务，无内建认证。** 默认只监听 `127.0.0.1`。若对外暴露，必须在反向代理层加认证/TLS，
> 因为提示词与会话接口具有写权限。

## 1. 输入事件

### 1.1 规范形式

`POST /v1/events`，`Content-Type: application/json`。

```json
{
  "event_id": "7d9d5f4e-6a4a-4b1e-9d09-6d7f6f3f1a2b",
  "session_id": "3f0c5f0e-6a4a-4b1e-9d09-6d7f6f3f1a2b",
  "parts": [
    {
      "modality": "text",
      "content": "这张图里是什么？",
      "metadata": {"locale": "zh-CN"}
    },
    {
      "modality": "image",
      "content": "data:image/png;base64,iVBORw0KGgo...",
      "metadata": {"mime_type": "image/png", "alt": "一张白色杯子的照片", "filename": "cup.png"}
    }
  ],
  "metadata": {
    "providerKind": "custom",
    "providerId": "a1b2c3d4-…",
    "modelName": "gpt-4o-mini",
    "model": "custom:a1b2c3d4-…:gpt-4o-mini",
    "prompt_profile": "default",
    "web_search": false,
    "locale": "zh-CN"
  }
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `event_id` | 否* | UUID。缺失时生成。*契约要求 UUID；重投相同值实现幂等 |
| `session_id` | 否 | UUID；不存在则创建；为空则新建会话 |
| `parts` | 是 | 1–16 个内容片段 |
| `parts[].modality` | 是 | `text` / `image` / `audio` / `file` / `video`；未知值按不支持处理 |
| `parts[].content` | 是 | 文本，或图片 data URL / http(s) URL；单片段 ≤ 12,000,000 字符 |
| `parts[].metadata` | 否 | 白名单辅助信息；`api_key/token/authorization/secret/password` 等键会被丢弃 |
| `metadata.providerKind` | 否 | `builtin` / `custom`；与 `providerId`、`modelName` 一起使用 |
| `metadata.providerId` | 否 | 内置 provider key 或自定义提供方 UUID |
| `metadata.modelName` | 否 | 上游模型名（自定义提供方为模型名，不是完整 ref） |
| `metadata.model` | 否 | 兼容旧调用的 canonical ref；新调用建议用上面三个字段 |
| `metadata.prompt_profile` | 否 | 本轮提示词配置 ID；显式不存在返回 404 |
| `metadata.web_search` | 否 | 本轮是否启用搜索；默认取会话设置 |
| `metadata.locale` | 否 | 模板变量与展示区域；最长 32 字符 |
| `metadata.session_id` | 否（不推荐） | 兼容旧调用的回退：合法 UUID 时使用并打 warning；**正确位置是事件顶层** |
| 其他 `metadata.*` | 否 | 当前不生效、不持久化、不写入日志；请勿依赖 |

#### metadata 生效键表

| 键 | 是否生效 | 说明 |
| --- | --- | --- |
| `providerKind` / `providerId` / `modelName` | 是 | 选择提供方/模型 |
| `model` | 是（兼容旧调用） | canonical ref；如 `openai:gpt-4o-mini` |
| `prompt_profile` | 是 | 提示词配置 ID |
| `web_search` | 是 | 本轮搜索开关 |
| `locale` | 是 | 模板变量/展示区域 |
| `session_id` | 兼容回退 | 推荐放顶层；放这里会 warning，并回退使用 |
| 其他键 | 否 | 忽略，不持久化 |

### 1.2 兼容形式

为了兼容上游尚未统一的封装，以下也接受：

```json
{"event_id":"…","modality":"text","content":"hello"}
```

```json
{"event_id":"…","content":[{"modality":"text","content":"hello"}]}
```

```json
{"event_id":"…","content":{"modality":"text","content":"hello"}}
```

### 1.3 图片

- `data:image/...;base64,...` 会直接传给支持视觉的厂商；原始 data URL 保存在本地存档。
- `http(s)` 图片 URL 会作为 URL 透传给 OpenAI-compatible / Anthropic；Gemini 适配器不会主动下载
  外部 URL，而是转为文字占位（避免 SSRF）。
- 模型无视觉能力时：
  1. 若同一回退链或已配置模型中有视觉模型，自动路由；
  2. 否则降级为文字。若 `metadata.alt/description/ocr_text` 存在则使用，否则给出明确占位说明。
- 音频/文件：支持 `metadata.transcript` / `metadata.extracted_text` / `metadata.text` 的文本化降级。

### 1.4 限制

- 请求体默认上限：20 MB（`server.max_body_bytes`）。
- 单片段上限：12 MB 字符（base64 解码后约 9 MB）。
- `parts` 最多 16 个。
- 超过限制返回 `413 PAYLOAD_TOO_LARGE` 或 `422 INVALID_INPUT`。

## 2. 输出事件（冻结）

JSON 模式返回单个对象；SSE 模式的 `delta` 与 `final` 帧数据也是这个对象。

```json
{
  "event_id": "0d6f8e5a-3f5c-4e7c-9a3b-2b5c6d7e8f90",
  "session_id": "3f0c5f0e-6a4a-4b1e-9d09-6d7f6f3f1a2b",
  "content": "……",
  "stream": false,
  "tags": {
    "emotion": "neutral",
    "type": "text",
    "intent": "reply"
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | UUID | 本回合输出事件 ID，同一回合所有帧一致；幂等重投时复用 |
| `session_id` | UUID | 会话 ID |
| `content` | string | `delta` 帧为增量；`final`/JSON 为完整文本。默认**不含**引用 URL；只有 `gateway.citations_in_content=true` 时才追加来源附录 |
| `stream` | bool | `delta` 为 `true`；`final`/JSON 为 `false` |
| `tags.emotion` | string | Contract v2：neutral, joy, excitement, comfort, tenderness, calm, sadness, apology, encouragement, surprise, frustration, anger；旧 `fear→sadness`、`confusion→frustration`、`gratitude→joy` |
| `tags.type` | string | text, code, list, table, json, mixed |
| `tags.intent` | string | reply, tool, linkage |

`intent=tool` 表示本轮实际使用了搜索结果；`linkage` 表示实际注入了保留记忆插件条目；否则 `reply`。

### 2.1 标签的口径

- 只对**模型输出**做分类，不分类用户输入。
- 分类器本地、确定性、无网络；不调用小模型，不增加延迟。
- `delta` 帧的标签基于“当前已累积文本”，可能变化；`final` 为最终标签。下游应使用 `final` 的标签。
- 标签不是内容安全审核结果。
- **Emotion contract v2**：对外只输出 12 类。`GET /v1/health`、`GET /v1/settings` 的
  `tag_contract` 与 SSE 响应头 `X-OmniNexus-Tag-Contract: 2` 可校验。
- 旧存档/旧调用中的 `fear/confusion/gratitude` 在输出边界自动归一；历史原文不变。

## 3. HTTP 模式

| 请求 | 行为 |
| --- | --- |
| `Accept: application/json`（默认） | 等待完整生成，返回一个 `OutputEvent` JSON |
| `Accept: text/event-stream` | 返回 SSE 流 |
| `?stream=1` / `?stream=0` | 显式覆盖 Accept |

JSON 响应头：

```
X-Event-Id: <output event uuid>
X-Session-Id: <session uuid>
X-Citations: <count>
X-OmniNexus-Tag-Contract: 2
X-Session-Created: true|false
X-Session-Legacy: true|false
```

JSON 错误响应统一为：

```json
{
  "error": {
    "code": "ALL_PROVIDERS_FAILED",
    "message": "All candidate models failed. Check provider configuration and try again.",
    "retryable": false,
    "details": {"attempts": [{"provider": "openai", "model": "openai:gpt-4o-mini", "code": "PROVIDER_ERROR"}]},
    "request_id": "…"
  }
}
```

错误信息不回显上游响应正文、密钥或对话内容。

## 4. SSE 语义

```
event: meta
data: {...}

event: route
data: {"provider":"openai","model":"openai:gpt-4o-mini","fallback":false}

event: delta
data: {"event_id":"…","session_id":"…","content":"增量","stream":true,"tags":{...}}

event: citations
data: {"citations":[{...}]}

event: final
data: {"event_id":"…","session_id":"…","content":"完整文本","stream":false,"tags":{...}}
```

| `event` | 是否契约事件 | 说明 |
| --- | --- | --- |
| `meta` | 否（辅助） | 会话、计划模型、检索/记忆状态、能力降级、丢弃历史条数、`session_created`、`legacy_session_id`、`tag_contract` |
| `route` | 否（辅助） | 实际选中厂商/模型、是否发生回退 |
| `delta` | **是** | 增量文本，`stream=true`，标签为临时值 |
| `citations` | 否（辅助） | 搜索结果与 URL；仅本轮有搜索时发送 |
| `final` | **是** | 完整文本，`stream=false`，最终标签 |
| `warning` | 否（辅助） | 部分输出后中断等非致命问题；随后仍有 `final` |
| `error` | 否（辅助） | 未产生任何文本即失败；没有 `final` |

连接保持：每 15 秒发送 SSE 注释 `: keep-alive`。客户端应忽略注释。

### 4.1 帧顺序

正常：

```
meta → route → delta* → citations? → final
```

首 Token 前全部失败：

```
meta → error
```

已产生部分文本后中断：

```
meta → route → delta* → warning → final（部分内容，stream=false）
```

### 4.2 为什么 final 是 `stream=false`

`delta` 是增量，`final` 是完整替换值。这样下游无需自己拼接或去重；若只消费 `delta`，也可按
`event_id` 聚合。默认 `final.content` 与 delta 拼接一致；若开启
`gateway.citations_in_content=true`，会追加“参考来源 / Sources”附录。

### 4.3 流式失败边界

- 首 Token 前失败：在候选链内重试/回退，对下游透明。
- 已输出文本后失败：**不会**把另一个模型的答案接在后面；保留部分内容、写入存档，发送 `warning`，
  再发送 `final`。下一轮对话可继续。
- 客户端断开：尽力保存已生成部分（`storage.keep_partial_on_disconnect`）。

## 5. 幂等

- 以输入 `event_id` 为幂等键，`messages.source_event_id` 唯一索引。
- 已完成的事件重投：返回同一 `output event_id` 与内容，不再调用模型。
- 未完成/正在生成的事件重投：可能执行第二回合；客户端应使用不同 `event_id` 发起新问题。
- 幂等缓存随会话存档删除而删除。

## 6. 会话与存档

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/sessions` | 创建会话 `{title?, model?, prompt_profile?, web_search?}` |
| GET | `/v1/sessions?limit=100&offset=0` | 列表（含消息数/最后消息摘要） |
| GET | `/v1/sessions/{id}` | 会话 + 消息 + 引用 |
| PATCH | `/v1/sessions/{id}` | 更新标题/模型/提示词/搜索开关 |
| DELETE | `/v1/sessions/{id}` | 级联删除消息与引用，204 |
| POST | `/v1/sessions/{id}/model` | `{providerKind, providerId, modelName, model}`；切换并持久化，上下文保留 |
| POST | `/v1/sessions/{id}/prompt` | `{ "profile_id": "…" }` |
| GET | `/v1/sessions/{id}/export?format=json\|md` | 下载完整存档 |
| GET | `/v1/sessions/{id}/citations` | 会话全部引用 |
| GET | `/v1/events/{event_id}/citations` | 单输出事件引用 |

导出使用 RFC 5987 文件名；中文标题不会导致响应头编码错误。JSON 导出包含
`format: omninexus.archive.v1`、会话、全部消息（含图片 data URL）、引用与时间戳。Markdown 导出
不内嵌图片数据，提示使用 JSON 导出。

## 7. 模型、厂商与设置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/models` | 内置 + 自定义模型、能力、providerKind/Id、验证状态、是否可用 |
| GET | `/v1/providers` | 厂商 Base URL、环境变量名、是否已配置；**不返回密钥值** |
| GET | `/v1/settings` | 页面初始化所需的模型/提示词/搜索/隐私摘要 |
| GET | `/v1/health` | 应用、存档、配置模型数、保留记忆与搜索状态 |
| GET | `/v1/healthz` | 轻量存活探针 |

`available=true` 表示**厂商已配置/自定义提供方已启用**，不保证网络可达；实际不可达会在请求时返回
可读错误（自定义提供方不跨提供方静默回退）。

### 7.1 自定义提供方

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/custom-providers` | 列表；Key 仅掩码 |
| POST | `/v1/custom-providers` | 新建 |
| GET | `/v1/custom-providers/{id}` | 详情 |
| PUT | `/v1/custom-providers/{id}` | 更新；`apiKey` 省略=不改，`clearApiKey=true`=清除 |
| DELETE | `/v1/custom-providers/{id}?confirm=true` | 删除；默认/使用中需确认 |
| PATCH | `/v1/custom-providers/{id}/enabled` | 启用/禁用 |
| POST | `/v1/custom-providers/test` | 未保存配置测试（可带 `id` 复用已保存 Key） |
| POST | `/v1/custom-providers/{id}/test` | 已保存配置测试 |
| POST | `/v1/custom-providers/discover-models` | 拉取未保存配置的 `/models` |
| POST | `/v1/custom-providers/{id}/discover-models` | 拉取已保存配置的 `/models` |
| GET/PUT | `/v1/provider-defaults` | 默认提供方/模型 |
| GET | `/v1/provider-config/export` | 导出（**不含 Key**） |
| POST | `/v1/provider-config/import` | 导入（无 Key，默认禁用） |
| GET | `/v1/provider-migration/candidates` | 不可用提供方的历史会话 |
| POST | `/v1/provider-migration/apply` | 手动迁移选中会话 |

创建请求：

```json
{
  "name": "我的中转",
  "baseUrl": "https://api.example.com/v1",
  "apiKey": "sk-…",
  "headers": {"X-Title": "OmniNexus"},
  "protocol": "openai-compatible",
  "timeoutSeconds": 10,
  "enabled": true,
  "models": [
    {
      "name": "gpt-4o-mini",
      "label": "GPT-4o mini",
      "capabilities": {
        "inputModalities": ["text", "image"],
        "contextWindow": 128000,
        "maxOutputTokens": 2048,
        "supportsStreaming": true
      }
    }
  ],
  "defaultModel": "gpt-4o-mini"
}
```

响应**不返回 `apiKey`**，返回 `apiKeyMasked`（如 `sk-****abcd`）与 `hasApiKey`。请求头禁止
`Authorization` / `x-api-key`，鉴权统一走 Key 字段。

测试结果：

```json
{
  "status": "success",
  "message": "连接成功，模型可用。",
  "levels": [
    {"level": "reachability", "status": "ok", "latencyMs": 42},
    {"level": "models", "status": "ok", "modelCount": 12},
    {"level": "model", "status": "ok", "latencyMs": 380}
  ],
  "multimodal": {"status": "verified"}
}
```

`status`：`success` / `unverified` / `auth_failed` / `unreachable` / `model_not_found` / `timeout`。
`unverified` 允许保存；`/models` 不可用时允许手动填写模型。

Base URL **严格拼接**：`base.rstrip("/") + "/models"`、`+ "/chat/completions"`，不自动补 `/v1`。
保存时拒绝包含 `/models` 或 `/chat/completions` 的地址。

## 8. 提示词接口

完整语义见 [提示词接口规范](prompt-api.md)。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/prompts/profiles` | 配置列表（含当前版本内容） |
| POST | `/v1/prompts/profiles` | 新建配置 |
| GET | `/v1/prompts/profiles/{id}` | 配置详情 + 版本历史 |
| PUT | `/v1/prompts/profiles/{id}` | 保存新版本并激活 |
| POST | `/v1/prompts/profiles/{id}/activate` | 激活指定版本 `{version}` |
| POST | `/v1/prompts/profiles/{id}/reset` | 创建并激活等于 version 1 的新版本 |
| DELETE | `/v1/prompts/profiles/{id}` | 删除自定义配置；内置 `default` 返回 409 |
| POST | `/v1/prompts/preview` | 用变量预览模板渲染 |

## 9. 搜索与保留记忆

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/search/status` | 搜索配置/可用性（不触发网络） |
| GET | `/v1/memory/status` | **Reserved** 插件状态；默认 `unavailable` |
| POST | `/v1/memory/probe` | 强制健康探测；禁用时立即返回，无网络 |

外部记忆的完整 wire contract 见 [保留记忆接口](memory-reserved.md)。它不在本期运行路径中。

## 10. 错误码

| code | 典型 HTTP | 含义 |
| --- | --- | --- |
| `INVALID_INPUT` | 422 | 请求结构/字段非法 |
| `PAYLOAD_TOO_LARGE` | 413 | 超过请求体上限 |
| `SESSION_NOT_FOUND` | 404 | 会话不存在 |
| `PROMPT_NOT_FOUND` | 404 | 提示词配置不存在 |
| `PROMPT_VERSION_NOT_FOUND` | 404 | 版本不存在 |
| `PROMPT_BUILTIN_PROTECTED` | 409 | 内置配置不可删除 |
| `PROMPT_INVALID` | 422 | 模板非法/超长/名称重复 |
| `UNKNOWN_MODEL` | 404 | 模型 ID 不在注册表 |
| `PROVIDER_UNAVAILABLE` | 409 | 提供方已删除/禁用/未配置，请重新选择 |
| `PROVIDER_IN_USE` | 409 | 提供方被会话引用，删除需确认 |
| `DEFAULT_PROVIDER_REQUIRED` | 409 | 默认提供方需先切换 |
| `CUSTOM_PROVIDER_NOT_FOUND` | 404 | 自定义提供方不存在 |
| `CUSTOM_PROVIDER_EXISTS` | 409 | 名称重复 |
| `CUSTOM_MODEL_NOT_FOUND` | 404 | 自定义模型不存在 |
| `UNSUPPORTED_PROTOCOL` | 422 | 仅支持 OpenAI 兼容协议 |
| `SECRET_STORE_ERROR` | 500 | 主密钥缺失/损坏/无法解密 |
| `CONFIG_IMPORT_INVALID` | 422 | 导入配置格式错误 |
| `PROVIDER_TEST_AUTH_FAILED` | 401 | 测试时鉴权失败 |
| `PROVIDER_TEST_UNREACHABLE` | 502 | 测试时地址不可达 |
| `PROVIDER_TEST_MODEL_NOT_FOUND` | 404 | 测试模型不存在 |
| `PROVIDER_TEST_TIMEOUT` | 504 | 测试超时 |
| `NO_PROVIDER_AVAILABLE` | 503 | 无任何已配置模型 |
| `PROVIDER_AUTH` | 502 | 厂商鉴权失败（密钥问题） |
| `PROVIDER_RATE_LIMITED` | 429/503 | 厂商或本地令牌桶限流 |
| `PROVIDER_TIMEOUT` | 504 | 连接/首 Token/读取超时 |
| `PROVIDER_BAD_RESPONSE` | 502 | 厂商返回空内容/非法流 |
| `ALL_PROVIDERS_FAILED` | 503 | 所有候选失败（`details.attempts`） |
| `GENERATION_INTERRUPTED` | 502 | 已输出部分后中断 |
| `INTERNAL_ERROR` | 500 | 未预期错误（不会泄露内部细节） |

上游错误正文不会透传；`details.attempts` 只含 provider/model/code/status/attempt。

## 11. 超时、重试与限流

默认值（`config/default.yaml`，可改）：

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| `gateway.connect_timeout_s` | 8 | 建立连接 |
| `gateway.first_token_timeout_s` | 45 | 首个文本 Token |
| `gateway.request_timeout_s` | 90 | 流式读取单次等待/总请求上限参考 |
| `gateway.max_retries` | 2 | 同厂商重试次数（仅首 Token 前） |
| `gateway.retry_backoff_s` | 0.6 | 指数退避基数，带抖动 |
| `gateway.rate_limit_rpm` | 120 | 每厂商进程内令牌桶；0 禁用 |
| `gateway.rate_limit_max_wait_s` | 1.5 | 等待配额上限，超过则回退 |
| `gateway.max_output_tokens` | 2048 | 输出上限（与模型上限取小） |
| `gateway.citations_in_content` | false | 是否把来源 URL 追加到正文；默认关闭，引用走 citations 事件/API |

厂商返回 `429` 时会尊重 `Retry-After`（上限 60 秒/单次等待 10 秒）。`401/403/400` 不重试，直接
进入下一候选。限流为进程内实现，多 worker 部署需外部限流。

## 12. 调用示例

### 12.1 JSON

```bash
curl -s http://127.0.0.1:8000/v1/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "11111111-1111-4111-8111-111111111111",
    "parts": [{"modality":"text","content":"用一句话解释量子纠缠"}],
    "metadata": {"model":"echo:echo"}
  }'
```

### 12.2 SSE

```bash
curl -N http://127.0.0.1:8000/v1/events \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{
    "event_id": "22222222-2222-4222-8222-222222222222",
    "parts": [{"modality":"text","content":"写一个 Python 列表推导式"}],
    "metadata": {"model":"echo:echo"}
  }'
```

### 12.3 切换模型并继续

```bash
curl -s -X POST http://127.0.0.1:8000/v1/sessions/<session_id>/model \
  -H 'Content-Type: application/json' \
  -d '{"model":"beta:one"}'
```

### 12.4 导出与删除

```bash
curl -OJ 'http://127.0.0.1:8000/v1/sessions/<session_id>/export?format=json'
curl -X DELETE http://127.0.0.1:8000/v1/sessions/<session_id>
```
