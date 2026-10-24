# 架构说明

## 1. 设计目标

1. **独立可运行**：只依赖 Python 与公开 PyPI 包，不依赖上游多模态模块或下游表达模块。
2. **本地优先**：对话、提示词、引用、标签默认只写本机 SQLite；不引入云端数据库或遥测。
3. **BYOK**：密钥只从环境变量读取，不进入 HTTP 请求、数据库、日志或仓库。
4. **契约稳定**：下游事件、记忆查询 JSON、提示词接口均按版本冻结；内部实现可替换。
5. **故障隔离**：任一增强能力（搜索、保留记忆、某个厂商）失效时，主对话链路仍可完成。

## 2. 组件

```mermaid
flowchart TB
    subgraph API[HTTP / SSE 层]
      E[/POST /v1/events/]
      S[/sessions/]
      M[/models / providers/]
      P[/prompts/]
      H[/health/]
    end
    subgraph Core[编排核心]
      O[Orchestrator]
      N[Input Normalizer]
      C[Context Builder]
      T[Classifier]
      G[Gateway Router]
    end
    subgraph Providers[厂商适配层]
      OA[OpenAI-compatible]
      AN[Anthropic]
      GE[Gemini]
      CO[Cohere]
      EC[Echo diagnostic]
    end
    subgraph Local[本地持久化]
      DB[(SQLite WAL<br/>sessions/messages/citations/prompts)]
    end
    subgraph Optional[可选增强]
      SR[Search RAG]
      MP[Reserved memory plugin<br/>default off]
    end

    E --> O
    S --> DB
    M --> G
    P --> DB
    O --> N --> DB
    O --> G
    O --> C
    O --> T
    O --> SR
    O -.-> MP
    G --> OA & AN & GE & CO & EC
    O --> DB
    H --> O
```

| 模块 | 路径 | 职责 |
| --- | --- | --- |
| API / SSE | `src/omninexus/api/` | 路由、请求校验、SSE 编码、错误映射、请求 ID |
| 编排 | `src/omninexus/orchestrator.py` | 一次对话回合的状态机、标签、持久化、流式帧 |
| 网关 | `src/omninexus/gateway/` | 能力协商、候选链、重试、限流、超时、厂商协议 |
| 提示词 | `src/omninexus/prompts/manager.py` | 档案、不可变版本、沙箱渲染、重置 |
| 会话 | `src/omninexus/sessions/service.py` | 会话生命周期、导出、最近窗口裁剪 |
| 存储 | `src/omninexus/storage/database.py` | SQLite schema、事务、WAL、线程执行 |
| 分类 | `src/omninexus/core/classifier.py` | 本地情感/类型/意图判定 |
| 搜索 | `src/omninexus/search/service.py` | 可配置搜索源、查询最小化、引用 |
| 保留记忆 | `src/omninexus/memory/` | 默认禁用插件、冻结契约、健康/熔断 |
| 配置 | `src/omninexus/config.py` | YAML + 环境变量、模型注册表、BYOK 映射 |
| 日志 | `src/omninexus/logging.py` | JSON/文本日志、密钥脱敏、文件可选 |

## 3. 一次回合的时序

```mermaid
sequenceDiagram
    autonumber
    participant U as 上游
    participant A as API
    participant O as Orchestrator
    participant D as SQLite
    participant G as Gateway
    participant P as Provider
    participant X as 下游

    U->>A: POST /v1/events (parts/metadata)
    A->>O: prepare(event)
    O->>D: 按 input event_id 查幂等
    alt 已处理
        O-->>A: replay final
        A-->>X: SSE final / JSON
    else 新事件
        O->>D: ensure session
        O->>G: plan(model, modalities)
        G-->>O: primary + candidates + 降级 parts
        O->>D: 写 user message
        opt 联网搜索
            O->>O: SearchService(当前查询)
        end
        opt 保留记忆插件（默认禁用）
            O->>O: query（超时/熔断，失败即跳过）
        end
        O->>O: 渲染提示词 + 记忆/检索块
        O->>O: Token 预算内裁剪历史
        O->>G: stream(plan, messages)
        G->>P: 厂商请求（重试/限流）
        P-->>G: SSE tokens
        G-->>O: ChatDelta
        O->>O: 累积分类 → tags
        O-->>X: event: delta（OutputEvent）
        O->>D: assistant + citations
        O-->>X: event: final（完整 OutputEvent）
        O-)D: best-effort writeback（仅启用插件）
    end
```

`prepare` 在返回 `StreamingResponse` 之前完成，因此未知模型、无可用厂商、非法输入等错误仍能返回
正常 HTTP 4xx/5xx JSON，而不是半截 SSE。

## 4. 存储结构

SQLite，默认 `data/omninexus.db`，WAL 模式，外键开启。

```sql
sessions(id, title, model, prompt_profile, web_search,
         created_at, updated_at, metadata)
messages(id, session_id→sessions, role, content, parts JSON,
         created_at, token_estimate, metadata JSON, source_event_id)
citations(id, event_id, session_id→sessions, url, title, snippet,
          rank, source, created_at)
prompt_profiles(id, name UNIQUE, description, builtin, active_version,
                created_at, updated_at)
prompt_versions(id, profile_id→prompt_profiles, version, system_template,
                change_note, content_hash, created_at,
                UNIQUE(profile_id, version))
```

- `messages.parts` 保存规范化的文本/图片/音频/文件片段；图片以 data URL 存储，导出时完整保留。
- `messages.source_event_id` 唯一索引用于**输入幂等**：同一 `event_id` 重投返回同一输出事件。
- `metadata` 只保存白名单字段（模型、标签、用量、能力原因等），不保存上游原始 metadata。
- 删除会话触发 `ON DELETE CASCADE`，同时删除消息与引用。
- 每次操作在线程池中打开连接；写操作由进程内 `asyncio.Lock` 串行化，读操作可并发。

## 5. 上下文窗口

`ContextBuilder` 采用保守估算：CJK 字符约 1 Token，其他字符约 4 字符/Token；图片按 850 Token 估算。

```text
可用预算 = 模型上下文窗口 - 最大输出 Token - 安全余量(512)
```

1. 系统提示词（含检索/记忆块）先占用预算。
2. 从最新消息向旧消息加入，直到预算用尽；丢弃更旧消息。
3. **当前用户消息永远保留**；如超预算则截断文本，图片至少保留最后一张。
4. 回退到上下文更小的模型时，网关再按该模型预算裁剪一次。
5. 被丢弃的消息仍完整保存在存档中，可通过导出查看。

这是 v1.1 的**正式记忆方案**，不依赖外部管线。

## 6. 网关与能力协商

`Gateway.plan(model_id, parts)`：

1. 读取模型注册表，构造候选链：主模型 → `fallbacks` → 全局 `fallback_models`。
2. 过滤未配置的厂商（缺密钥/禁用）；若整条链都不可用，但存在其他已配置模型，则重路由到该模型，
   标记 `requested_unavailable`。
3. 检查输入模态：若主模型不支持图片，优先在同链中寻找支持视觉的模型（`capability_route`）；
   找不到则把图片/音频/文件转成带说明的文字（`capability_degraded`），对话不失败。
4. 若主模型只支持文本、但异常回退到另一个文本模型，网关会在发送前移除图片并补文字占位。
5. 执行时按候选链尝试；每个厂商内部先按 `max_retries` 重试，再切换下一候选。

详细策略与流式中断语义见 [降级方案](degradation.md)。

## 7. 标签

标签在本地、确定性生成，随每个 `delta` 与 `final` 事件下发：

- `emotion`：joy / sadness / anger / fear / surprise / confusion / frustration / gratitude / neutral
- `type`：text / code / list / table / json / mixed
- `intent`：reply / tool / linkage
  - `tool`：本轮实际使用了联网搜索结果
  - `linkage`：本轮实际注入了保留记忆插件的条目
  - `reply`：普通回复

标签是**内容标签**而非安全分类；不替代内容审核。

## 8. 并发与性能

- FastAPI + uvicorn，单进程异步 I/O；SQLite 写入串行但很快。
- 每个厂商共享一个 `httpx.AsyncClient`，连接复用；请求级超时。
- 限流为**进程内令牌桶**（每厂商），多进程/多实例部署时需外部限流。
- 内存中的健康探测/熔断状态只影响保留记忆插件，不进入正式路径。
- 大图片以 base64 存 SQLite，建议单图 < 8 MB；生产部署可替换为本地文件存储适配器。

## 9. 关键设计决策

| 决策 | 理由 |
| --- | --- |
| 提示词与模型解耦 | 同一条消息可在不同厂商间切换；提示词版本独立于模型 ID |
| RAG 而非强制工具调用 | 各厂商工具协议差异大；预检索在所有模型上行为一致，且引用可控 |
| 最终帧 `stream=false` | 下游可以“替换”而不是“追加”，避免重复正文；增量帧 `stream=true` |
| 默认无系统提示词 | 不在仓库内置人设；正式人设由产品完成后填写 |
| 保留记忆接口但不接入 | v1.1 明确取消协作；存档 + 最近窗口是正式路径，插件保留供未来使用 |
| 本地无认证 | 默认只监听回环地址；对外部署必须加外层认证 |

## 10. 扩展点

- **新厂商**：在 `providers` 中增加 OpenAI-compatible 配置，或实现 `BaseProvider`。
- **新模型**：在 `models` 中增加条目与 `fallbacks`；无需改代码。
- **新分类标签**：修改 `classifier.py` 与枚举，并更新契约版本。
- **新搜索源**：实现 `SearchProvider` 并注册。
- **记忆管线**：实现 `MemoryPlugin` 或使用 `HttpMemoryPlugin`，默认禁用。
- **存储后端**：`Database` 与 `SessionService` 隔离，可替换为其他本地存储。
