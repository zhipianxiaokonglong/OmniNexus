# 保留扩展：外部记忆管线（Reserved for future use）

> **状态：Reserved for future use（保留，不在本期交付范围）**
>
> 原外部记忆管线协作已取消。本期唯一正式记忆机制是**本地对话存档 + 最近对话窗口**。
> 本接口以插件/适配器形式保留，**默认禁用**；未接入时健康探测固定返回 `unavailable`，对主流程
> 零影响。接口、适配器、契约与 mock 验证不得在后续迭代删除。

## 1. 为什么保留

未来可能接入一套本地记忆管线：小模型分析输入 → 结构化查询 JSON → 检索上下文库 → 按 Token 预算
打包 → 生成后异步写回。提前冻结 wire contract 可以避免未来重新设计接口，同时不影响本期验收。

本期保证：

- `config/default.yaml` 中 `memory.enabled: false`；
- 未配置时使用 `DisabledMemoryPlugin`，不发起任何网络 I/O；
- `/v1/memory/status` 与 `/v1/health` 显示 `unavailable`（`configured:false`）；
- 主流程的上下文只来自 SQLite 中的最近窗口；
- 插件可以被未来代码或测试直接调用（`MemoryPlugin` 接口）。

## 2. 代码结构

| 文件 | 作用 |
| --- | --- |
| `src/omninexus/memory/contract.py` | 冻结版本、查询构造、Pydantic 响应模型、错误码 |
| `src/omninexus/memory/plugin.py` | `MemoryPlugin` 抽象、`DisabledMemoryPlugin`、`HttpMemoryPlugin` |
| `src/omninexus/memory/__init__.py` | 对外导出 |
| `src/omninexus/orchestrator.py` | 仅在插件 `configured` 时查询/写回；失败时回退 |
| `tests/test_memory_reserved.py` | 禁用零影响、超时、500、熔断、写回、直接调用 mock |

## 3. Wire contract v1.0

### 3.1 版本与头

- `contract_version`: `"1.0"`
- 每个请求头：`X-OmniNexus-Memory-Contract: 1.0`
- 示例 Base URL：`http://127.0.0.1:8788`（必须由用户配置；不会自动发现）
- 仅允许 `http`/`https`；建议只监听回环地址。
- 鉴权（可选）：`Authorization: Bearer <OMNI_MEMORY_TOKEN>`。
- 内容类型：`application/json; charset=utf-8`。

### 3.2 健康探测

```http
GET {base_url}/health
```

成功：

```json
{
  "status": "ok",
  "contract_version": "1.0",
  "capabilities": ["query", "write"]
}
```

- 只有 HTTP 200 且 `status ∈ {ok, healthy, ready}` 视为可用。
- `contract_version` 若存在，主版本必须为 `1.x`；否则判定契约不兼容。
- 超时：`memory.health_timeout_ms`，默认 600 ms。
- 失败不计入对话失败；进入不可用/熔断状态。

### 3.3 查询

```http
POST {base_url}/v1/memory/query
Content-Type: application/json
X-OmniNexus-Memory-Contract: 1.0
```

请求体（**冻结**）：

```json
{
  "query": {
    "keywords": ["杭州", "项目", "Alpha"],
    "entities": ["Alpha", "杭州"],
    "time_range": {
      "start": "2026-10-10T00:00:00+08:00",
      "end": "2026-10-24T17:30:00+08:00"
    },
    "top_k": 5,
    "priority": "high"
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `query.keywords` | string[] | 最多 10 个；本地关键词提取，不含历史 |
| `query.entities` | string[] | 最多 8 个；引号/书名号/大写词/@/URL |
| `query.time_range` | object \| null | 仅当文本含时间线索时提供；ISO 8601 带时区 |
| `query.top_k` | int | 1–10，默认 5 |
| `query.priority` | `high` \| `normal` | 出现“记得/上次/之前/remember”等线索时为 high |

查询只使用**当前用户文本**；不发送会话 ID、历史、系统提示词或标签。构造器见
`memory/contract.py::build_query`。

成功响应（冻结）：

```json
{
  "contract_version": "1.0",
  "items": [
    {
      "id": "mem_01",
      "content": "用户喜欢无糖绿茶。",
      "score": 0.92,
      "source": "archive",
      "timestamp": "2026-10-20T12:00:00+08:00",
      "metadata": {"kind": "preference"}
    }
  ],
  "tokens": 42
}
```

- `items` 最多取 `memory.max_items`（默认 8），每条 `content` 截断至 4000 字符。
- `items` 只作为不可信上下文注入，不作为指令。
- 响应体非对象/字段类型错误 → `MEM_SCHEMA`，客户端回退最近窗口。

错误响应：

```json
{
  "error": {
    "code": "MEM_TIMEOUT",
    "message": "query timed out",
    "request_id": "…"
  }
}
```

### 3.4 写回

```http
POST {base_url}/v1/memory/write
Content-Type: application/json
X-OmniNexus-Memory-Contract: 1.0
```

```json
{
  "contract_version": "1.0",
  "session_id": "…",
  "event_id": "…",
  "items": [
    {"role": "user", "content": "…", "created_at": "ISO8601"},
    {"role": "assistant", "content": "…", "created_at": "ISO8601",
     "metadata": {"tags": {"emotion": "neutral", "type": "text", "intent": "reply"}}}
  ]
}
```

- 由编排器在最终答案落库后触发 `asyncio.create_task`，**不阻塞响应**。
- 失败只记日志，不重试到主流程；未来可实现持久化 outbox。
- 成功状态码：200/201/202。

### 3.5 错误码（冻结）

| code | HTTP | 含义 |
| --- | --- | --- |
| `MEM_OK` | 200 | 成功 |
| `MEM_BAD_REQUEST` | 400 | 查询/写回 payload 非法 |
| `MEM_UNAUTHORIZED` | 401 | token 缺失/无效 |
| `MEM_RATE_LIMITED` | 429 | 记忆服务限流 |
| `MEM_TIMEOUT` | 504 | 超时 |
| `MEM_UNAVAILABLE` | 503 | 服务不可用/未就绪 |
| `MEM_INTERNAL` | 500 | 内部错误 |
| `MEM_SCHEMA` | 422 | 响应不符合本契约 |

HTTP 错误也会被映射为不可用；任何错误都不会抛到主对话。

## 4. 超时与熔断

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `memory.enabled` | `false` | 总开关 |
| `memory.base_url` | `""` | 为空即未配置 |
| `memory.health_timeout_ms` | 600 | 健康探测 |
| `memory.query_timeout_ms` | 1200 | 查询 |
| `memory.write_timeout_ms` | 1500 | 写回 |
| `memory.health_interval_s` | 30 | 健康缓存 |
| `memory.circuit_failures` | 3 | 连续失败阈值 |
| `memory.circuit_cooldown_s` | 30 | 熔断冷却 |
| `memory.top_k` | 5 | 查询条数 |
| `memory.max_items` | 8 | 注入上限 |
| `memory.max_context_tokens` | 700 | 上下文预算（预留） |

行为：

- 禁用：`query` 直接返回空，零 I/O、零延迟。
- 未配置但 `enabled=true`：状态 `unavailable`，不探测。
- 健康失败/超时：状态 `unavailable`。
- 连续失败达到阈值：`circuit_open`，冷却期内直接跳过，不发请求。
- 编排器外层还有 `query_timeout + 200ms` 的 `asyncio.timeout` 保护。
- 错误/延迟不改变主回答；`meta.memory.reason` 可供观测。

## 5. 启用方式（未来开发/联调）

```yaml
memory:
  enabled: true
  base_url: http://127.0.0.1:8788
  auth_token_env: OMNI_MEMORY_TOKEN
```

```powershell
$env:OMNI_MEMORY_ENABLED = "1"
$env:OMNI_MEMORY_BASE_URL = "http://127.0.0.1:8788"
$env:OMNI_MEMORY_TOKEN = "local-dev-token"   # 如服务要求
python -m omninexus
```

默认值不要改成 `true` 发布；本期验收要求默认禁用。

## 6. Mock 验证

```powershell
python -m pytest tests/test_memory_reserved.py -q
```

覆盖：

- 禁用时无任何 `memory.test` 请求，事件成功，`/v1/memory/status` 为 `unavailable`；
- 启用后 mock 返回条目，provider 的 system 消息包含 `<external_memory>`；
- 查询请求体严格符合 `{query:{keywords,entities,time_range,top_k,priority}}`；
- 写回 payload 含 user/assistant 两条；
- 查询 500 / 超时 / 熔断时回答仍成功；
- 直接调用 `HttpMemoryPlugin.health/query`（未来扩展入口）。

## 7. 变更控制

- `MEMORY_CONTRACT_VERSION` / 路径 / 字段 / 错误码为冻结内容。
- 任何变更必须：
  1. 联合评审并记录理由；
  2. 升级主/次版本（例如 1.0 → 1.1），保留旧版本适配；
  3. 更新本文档、契约测试与 changelog；
  4. 先提供兼容层，再切换默认值。
- **不得单方面修改**，也不得在后续迭代删除本接口或测试。

## 8. 与正式存档的关系

| 维度 | 本地存档（正式） | 外部记忆插件（保留） |
| --- | --- | --- |
| 默认 | 启用 | 禁用 |
| 存储 | 本机 SQLite | 外部本地服务 |
| 检索 | 最近窗口 + Token 裁剪 | 结构化查询 + 预算打包 |
| 故障影响 | 主路径 | 零影响/可回退 |
| 导出/删除 | 支持 | 由外部服务负责 |
| 验收 | 本期必须 | mock 验证 + 零影响 |
