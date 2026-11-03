# 兼容性矩阵

## 1. 版本

| OmniNexus | Event contract | Emotion tag contract | Memory contract | Python |
| --- | --- | --- | --- | --- |
| 0.3.x | 1.0（字段不变） | **2**（12 类） | 1.0（Reserved） | 3.11+ |
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

## 5. 端口

默认 `8000`。跨仓库共存时请在启动脚本显式指定端口并检查占用，不使用隐式默认。
