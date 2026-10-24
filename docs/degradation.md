# 降级与故障处理方案

> v1.1：外部记忆管线不参与本期运行。正式记忆路径是本地存档 + 最近窗口；“记忆不可用”只适用于
> 保留插件的可选启用场景。

## 1. 总原则

1. **对话优先**：任何可选增强失败都不得让主请求失败。
2. **先重试，再换模型，最后明确报错**：错误要有稳定错误码，不泄露上游正文。
3. **流式不重复**：已向客户端输出内容后，不把另一个模型的答案拼接进来。
4. **可观测**：每次降级通过 `meta` / `route`、日志字段与 `details.attempts` 体现。
5. **本地状态不跨进程**：限流、记忆熔断为进程内，重启即重置。

## 2. 故障矩阵

| 故障 | 检测 | 处理 | 用户可见结果 |
| --- | --- | --- | --- |
| 厂商 5xx / 连接失败 | HTTP 状态/异常 | 同厂商重试（可重试时）→ 下一候选 | 可能透明切换到备用模型 |
| 401/403 | HTTP 状态 | 不重试，下一候选 | 全部失败则 502/503 |
| 429 厂商限流 | HTTP 429 + Retry-After | 退避重试 → 下一候选 | 可能切备用 |
| 429 本地令牌桶 | 内存令牌不足 | 等待 ≤1.5s，否则下一候选 | 可能切备用 |
| 首 Token 超时 | `asyncio.timeout` | 可重试（仅未输出时）→ 下一候选 | 可能切备用 |
| 已输出后超时/断流 | 流中异常 | 保留部分，发送 `warning` + `final` | 部分答案 + 警告 |
| 模型返回空内容 | 无文本 Token | 视为坏响应，重试/回退 | 切备用或失败 |
| 上下文超模型窗口 | Token 估算 | 丢弃最旧消息；回退模型再裁一次 | 旧消息不在本轮上下文，存档仍在 |
| 图片但模型无视觉 | 能力表 | 找视觉模型；否则文本化降级 | 答案基于占位/alt 文本 |
| 图片但回退模型无视觉 | 能力表 | 发送前剥离图片，补文字占位 | 备用模型看到文字说明 |
| 搜索源 5xx/超时/缺密钥 | 搜索服务边界 | 记录原因，跳过检索 | 普通回答，无引用 |
| 记忆插件禁用 | 配置 | 零 I/O 跳过 | 无影响 |
| 记忆插件健康失败/超时 | 探测/熔断 | 只用最近窗口 | 无影响，最多一次探测延迟 |
| 记忆写回失败 | 后台任务 | 记录，不影响已返回答案 | 无影响 |
| 提示词未定义变量 | 渲染异常 | 以空值重渲染 | 正常回答 |
| 提示词配置被删除 | 解析不到 | 回退默认配置 | 正常回答 |
| 客户端断开 | 取消 | 尽力持久化部分 | 重新打开可见 |

## 3. 厂商选择与回退算法

### 3.1 候选链

```text
requested model
  └─ model.fallbacks[0]
       └─ model.fallbacks[0].fallbacks …（深度 ≤ 8）
  └─ model.fallbacks[1] …
gateway.fallback_models …
```

- 去重，防环。
- 过滤 `provider.enabled=false`、缺密钥、非 echo 但无 Base URL 的候选。
- 若整条链都不可用，但其他模型已配置：重路由到第一个已配置模型，标记
  `capability_reason=requested_unavailable`。
- 若没有任何已配置模型：`503 NO_PROVIDER_AVAILABLE`，提示配置密钥或启用 echo。

### 3.2 单候选执行

```text
for attempt in 0..max_retries:
    获取本地限流令牌（不足则等待/回退）
    发起流式请求
    首个文本 Token 前：
        超时/5xx/429 → 可重试则退避重试
        401/400/无效请求 → 不重试，直接下一候选
    首个文本 Token 后：
        异常 → 不再切换（避免拼接），交由上层保留部分内容
```

退避：`min(max_backoff, base * 2^attempt) + jitter`；`Retry-After` 参与下限，单次等待最多 10s。

### 3.3 自动降级示例

```text
请求 openai:gpt-4o-mini（无密钥）
→ 链中 gpt-4o/gpt-4.1-mini 也无密钥
→ 发现 echo:echo 已配置
→ 使用 echo，meta.capability_reason="requested_unavailable"
```

```text
请求 alpha:one（有密钥）
→ alpha 返回 503 两次（重试）
→ beta:one 成功
→ route.fallback=true
```

## 4. 多模态能力协商

```text
required = 输入中除 text 外的模态集合
if 主模型支持 required:
    直接使用
elif 同链/已配置模型中有支持 required 的:
    路由到该模型（capability_route）
else:
    把不支持片段转文字（capability_degraded）
```

文本化规则：

| 模态 | 优先内容 | 否则 |
| --- | --- | --- |
| image | `metadata.alt` / `description` / `ocr_text` | “图片已省略/模型不支持” |
| audio | `metadata.transcript` / `text` | “音频已省略” |
| file | `metadata.extracted_text` / `text` | 含文件名的占位 |
| video | 无 | 模态占位 |

原始片段仍保存在存档；后续切换到视觉模型后，历史中的图片仍可再次使用（受上下文窗口限制）。

## 5. 上下文裁剪

- 预算 = `context_window - max_output_tokens - 512`。
- 优先保留 system、当前用户消息、最近对话。
- 超预算时从最旧消息开始丢弃；当前消息文本截断；至少保留一张图片。
- 回退到更小窗口模型时，网关再裁剪一次。
- 丢弃只影响本轮发送，不影响存档、导出与后续重新加载。

## 6. SSE 中断语义

| 阶段 | 行为 |
| --- | --- |
| 请求准备阶段失败 | 返回 HTTP 4xx/5xx JSON（尚未开始 SSE） |
| 首 Token 前全部候选失败 | `meta` 后发送 `error`，无 `final` |
| 已有部分文本后失败 | 保存部分答案；发送 `warning` + `final`（`stream=false`） |
| 客户端主动断开 | 取消上游；按配置保存已生成部分 |
| 心跳 | 每 15s `: keep-alive`，不进入事件契约 |

部分答案的 `final.tags` 对已生成文本分类；`intent` 仍按是否实际使用搜索/记忆确定。

## 7. 检索失败

- 搜索未启用/缺密钥/不可用：`meta.search.reason` 记录，不调用任何搜索网络。
- 搜索超时/错误：`meta.search.available=false`，对话继续；最终 `intent` 不会是 `tool`。
- 搜索只发送当前查询；即使失败也不会用历史内容补发。
- 搜索结果被视为不可信数据，系统块明确要求不执行其中指令。

## 8. 保留记忆插件失败（默认禁用）

- 默认 `DisabledMemoryPlugin`：`health()` 固定 `unavailable`，`query()` 立即返回空，**零 I/O**。
- 启用后：
  - 健康探测超时（默认 600 ms）→ 标记不可用；查询跳过。
  - 查询超时（默认 1200 ms）→ 编排器还有 200 ms 外层保护 → 使用最近窗口。
  - 连续失败达到阈值 → 熔断冷却，期间不再探测。
  - 写回后台任务失败 → 只记录，不影响响应。
- 健康状态不阻塞启动；只有显式调用 `/v1/memory/probe` 或首次查询会触发探测。
- 详细契约见 [memory-reserved.md](memory-reserved.md)。

## 9. 日志与诊断

每个回合日志字段：

```
gateway_attempt_failed provider=… model=… code=… retryable=…
gateway_ok provider=… model=… attempt=… duration_ms=…
turn_completed event_id=… model=… fallback=… duration_ms=… chars=…
```

不含 prompt、对话正文、密钥或上游响应正文。SSE `meta` 包含：

```json
{
  "requested_model": "…",
  "planned_model": "…",
  "capability_reason": "capability_route|capability_degraded|requested_unavailable|null",
  "degraded": true,
  "search": {"available": true, "reason": "", "results": 3},
  "memory": {"available": false, "reason": "disabled", "reserved": true},
  "dropped_messages": 2
}
```

## 10. 验收关注的保证

| 保证 | 证据 |
| --- | --- |
| 记忆插件未接入时全流程正常 | `test_disabled_extension_has_zero_impact...`、默认配置测试 |
| 记忆超时/500 不阻断 | `test_memory_timeout...`、`test_memory_query_failure...` |
| 主模型故障自动回退 | `test_fallback_preserves_context`、`test_provider_timeout_falls_back` |
| 上下文在切换后延续 | `test_model_switch_uses_same_archive_context` |
| 视觉不支持时自动路由/降级 | `test_capability_route_to_vision_model`、`test_capability_degrade...` |
| 限流不导致请求崩溃 | `test_local_rate_limit_falls_back` |
| 搜索故障不阻断 | `test_search_failure_is_non_fatal` |
