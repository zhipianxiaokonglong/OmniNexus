# 兼容性矩阵

## 1. 版本

| OmniNexus | Event contract | Emotion tag contract | Memory contract | Python |
| --- | --- | --- | --- | --- |
| 0.3.3 | 1.0（字段不变） | **2**（12 类） | 1.0（Reserved） | 3.11+ |
| 0.3.2 | 1.0（字段不变） | **2**（12 类） | 1.0（Reserved） | 3.11+ |
| 0.3.1 | 1.0（字段不变） | 2（12 类） | 1.0（Reserved） | 3.11+ |
| 0.3.0 | 1.0（字段不变） | 2（12 类） | 1.0（Reserved） | 3.11+ |
| 0.2.x | 1.0 | 1（9 类，历史） | 1.0（Reserved） | 3.11+ |
| 0.1.x | 1.0 | 1（9 类，历史） | — | 3.11+ |

## 2. Emotion contract v2

对外输出固定 12 类：

```text
neutral, joy, excitement, comfort, tenderness, calm,
sadness, apology, encouragement, surprise, frustration, anger
```

旧标签别名：

| 旧 | 新 |
| --- | --- |
| `fear` | `sadness` |
| `confusion` | `frustration` |
| `gratitude` | `joy` |

升级到 0.3.0 后：

- 新输出只会出现 12 类；
- 历史存档原文不变，重放/导出标签按 v2 归一；
- 下游如已接受 12 类无需映射；如仍接受旧 9 类，需增加上述三条别名。

校验：

```text
GET /v1/health      → tag_contract.version == "2"
GET /v1/settings    → tag_contract.version == "2"
SSE / JSON response → X-OmniNexus-Tag-Contract: 2
SSE meta            → tag_contract
```

## 3. Event contract 1.0

下游 `OutputEvent` 的必需字段仍是：

```text
event_id, session_id, content, stream, tags{emotion,type,intent}
```

0.3.0 的变化：

- `final.content` 默认不再包含来源 URL（`gateway.citations_in_content: false`）；
- 如需正文内嵌来源，显式开启该配置；
- 引用仍可通过 SSE `citations` 事件、`GET /v1/events/{id}/citations` 获取。

## 4. Session 信号

| 信号 | 位置 | 含义 |
| --- | --- | --- |
| `session_created` | SSE `meta` / `X-Session-Created` | 本轮是否新建会话 |
| `legacy_session_id` | SSE `meta` / `X-Session-Legacy` | 是否从 `metadata.session_id` 兼容回退 |
| `session_id` | 事件顶层（推荐） | 客户端应持久化 UUID 并放顶层 |

`metadata.session_id` 只在顶层缺失时回退；会写 warning（只含 event_id），不写对话内容。

### 4.1 自定义提供方模型上限

- 单个提供方最多 **200** 个模型（0.3.2 起错误信息可读）；
- `discover-models` 返回上游全集，设置页按剩余容量截断；
- 201 条及以上返回 422 `INVALID_INPUT`，`message` 含“最多 200 个模型”。

### 4.2 默认提供方/模型

- 新建会话会持久化 `GET/PUT /v1/provider-defaults` 的结果；
- `gateway.default_model` 仅作为**未设置用户默认**时的初始回退，不再遮蔽设置页默认；
- **旧格式会话**（0.3.1 之前创建、拆分字段为 NULL）若**尚无模型回答**，首次对话会自动迁移到
  当前可用默认，`meta.recovered_default=true`；
- **已有回答的会话**保持原模型，不静默迁移；其提供方不可用时返回 `409
  PROVIDER_UNAVAILABLE`，`message` 说明当前模型与建议目标，`details.suggested` 给出
  `providerKind/providerId/model`；
- 显式创建并带拆分字段的会话，即使提供方被禁用也不会被自动改写。

## 5. 版本语义（跨仓库契约）

| 字段 | 位置 | 含义 | 何时变化 |
|---|---|---|---|
| `service_version` | `/v1/healthz`、`/v1/contract` | 模块/包版本 | 每次发布 |
| `contract_version` | `/v1/healthz` | Event 契约版本 | 仅破坏性变更 |
| `tag_contract` | `/v1/healthz` | Emotion 契约版本（当前 `2`） | 仅破坏性变更 |
| `contracts.*.version` | `/v1/contract` | 各契约独立版本 | 各自破坏性变更 |

**不要混用**：`service_version` 变不代表契约不兼容。

不兼容处理策略（`/v1/contract` 的 `compatibility.policy` =
`major_reject_minor_degrade`）：

- **主版本不兼容** → 消费方应拒绝请求并告警；
- **次版本新增**（如新增情绪标签）→ 消费方降级并告警，不中断对话。

`/v1/contract` 为只读端点，不读写会话、不返回密钥。

## 6. 情绪标签以 12 类为准

`/v1/contract` 的 `contracts.emotion.labels` 即唯一权威列表。旧 9 类视为历史版本，
消费方应通过 `legacy_aliases` 映射，不要反向要求生产方收敛。

## 7. 端口

默认 `8000`。跨仓库共存时请在启动脚本显式指定端口并检查占用，不使用隐式默认。
