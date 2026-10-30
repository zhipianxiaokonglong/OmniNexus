# 厂商接入与推荐清单

## 1. 原则

- **BYOK（Bring Your Own Key）**：OmniNexus 只提供适配器与推荐，不提供密钥、账户、额度或代管。
- 内置厂商密钥只从**本机环境变量**读取（`api_key_env`）；自定义提供方 Key 经本地 AES-GCM 加密
  落库，不返回完整值、不写日志、不导出。
- 所有 Base URL 与模型 ID 都可配置；厂商接口和模型会变化，请以厂商官方文档为准。
- 默认注册表中的能力（文本/视觉、上下文窗口）是工程默认值，不是厂商 SLA；可自行修正。

## 1.1 自定义提供方（v1.2）

设置页 **设置 · 模型提供方** 可添加任意 OpenAI-compatible 服务：

| 字段 | 说明 |
| --- | --- |
| 名称 | 用户自定义，唯一 |
| Base URL | **严格拼接**，不自动补 `/v1`；placeholder 为 `https://api.example.com/v1` |
| API Key | 可留空（本地无鉴权）；AES-GCM 加密落库，页面只显示 `sk-****abcd` |
| 模型 | 手动添加或从 `GET {baseUrl}/models` 拉取 |
| 能力 | `inputModalities`、`contextWindow`、`maxOutputTokens`、`supportsStreaming`；可用预设 |
| 请求头 | 可选非敏感头；禁止 `Authorization` / `x-api-key` |
| 协议 | 本期仅 `openai-compatible`；其他值返回 422 |
| 测试超时 | 默认 10 秒，可按提供方设置 |

Base URL 示例：

```text
https://api.example.com          → /chat/completions
https://api.example.com/v1       → /v1/chat/completions
https://api.example.com/v1/      → /v1/chat/completions
```

只有纯域名时，页面会**非强制**提示是否补 `/v1`；后端不会自动补。

自定义提供方失败不跨提供方静默回退；同一提供方内可因图片输入路由到声明的视觉模型。
历史会话不会静默迁移。

## 2. 推荐清单

| 厂商 | provider key | 协议 | Base URL | 密钥环境变量 |
| --- | --- | --- | --- | --- |
| OpenAI | `openai` | OpenAI-compatible | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| Anthropic | `anthropic` | Anthropic Messages | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` |
| Google Gemini | `gemini` | Gemini generateContent | `https://generativelanguage.googleapis.com` | `GEMINI_API_KEY` |
| Mistral AI | `mistral` | OpenAI-compatible | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` |
| xAI | `xai` | OpenAI-compatible | `https://api.x.ai/v1` | `XAI_API_KEY` |
| Cohere | `cohere` | Cohere Chat v2 | `https://api.cohere.com` | `COHERE_API_KEY` |
| DeepSeek | `deepseek` | OpenAI-compatible | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` |
| 阿里通义千问（百炼） | `dashscope` | OpenAI-compatible | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` |
| 字节豆包（火山方舟） | `volcengine` | OpenAI-compatible | `https://ark.cn-beijing.volces.com/api/v3` | `ARK_API_KEY` |
| 智谱 GLM | `zhipu` | OpenAI-compatible | `https://open.bigmodel.cn/api/paas/v4` | `ZHIPU_API_KEY` |
| 月之暗面 Kimi | `moonshot` | OpenAI-compatible | `https://api.moonshot.cn/v1` | `MOONSHOT_API_KEY` |
| 百度文心千帆 | `qianfan` | OpenAI-compatible | `https://qianfan.baidubce.com/v2` | `QIANFAN_API_KEY` |
| 讯飞星火 | `spark` | OpenAI-compatible | `https://spark-api-open.xf-yun.com/v1` | `SPARK_API_KEY` |
| 腾讯混元 | `hunyuan` | OpenAI-compatible | `https://api.hunyuan.cloud.tencent.com/v1` | `HUNYUAN_API_KEY` |
| MiniMax | `minimax` | OpenAI-compatible | `https://api.minimax.chat/v1` | `MINIMAX_API_KEY` |
| OpenRouter | `openrouter` | OpenAI-compatible | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| Ollama（本地） | `ollama` | OpenAI-compatible | `http://127.0.0.1:11434/v1` | 无 |
| LM Studio（本地） | `lmstudio` | OpenAI-compatible | `http://127.0.0.1:1234/v1` | 无 |
| vLLM（本地） | `vllm` | OpenAI-compatible | `http://127.0.0.1:8000/v1` | 无 |
| llama.cpp（本地） | `llamacpp` | OpenAI-compatible | `http://127.0.0.1:8080/v1` | 无 |

### 2.1 默认模型示例

以下只是注册表默认值，**不是强制版本**。国内厂商常使用部署 ID/端点 ID（例如火山方舟、千帆），
请替换 `model` 字段为账户中实际可用的 ID。

| provider | 示例模型 | 模态（默认） |
| --- | --- | --- |
| `openai` | `gpt-4o-mini`, `gpt-4o`, `gpt-4.1-mini` | 文本 + 图片 |
| `anthropic` | `claude-sonnet-4-20250514`, `claude-3-5-haiku-latest` | 文本 + 图片 |
| `gemini` | `gemini-2.5-flash`, `gemini-2.5-pro` | 文本 + 图片 |
| `mistral` | `mistral-small-latest`, `pixtral-12b-2409` | 文本 / 文本+图片 |
| `xai` | `grok-3-mini`, `grok-2-vision-1212` | 文本 / 文本+图片 |
| `cohere` | `command-a-03-2025`, `command-r-plus-08-2024` | 文本 |
| `deepseek` | `deepseek-chat`, `deepseek-reasoner` | 文本 |
| `dashscope` | `qwen-plus`, `qwen-max`, `qwen-vl-plus` | 文本 / 文本+图片 |
| `volcengine` | `doubao-1-5-pro-32k-250115`, `doubao-1-5-vision-pro-32k-250115` | 文本 / 文本+图片 |
| `zhipu` | `glm-4-plus`, `glm-4v-plus-0111` | 文本 / 文本+图片 |
| `moonshot` | `moonshot-v1-8k`, `moonshot-v1-32k`, `kimi-latest` | 文本 |
| `qianfan` | `ernie-4.0-8k` | 文本 |
| `spark` | `4.0Ultra` | 文本 |
| `hunyuan` | `hunyuan-turbos-latest`, `hunyuan-t1-latest` | 文本 |
| `minimax` | `MiniMax-Text-01`, `abab6.5s-chat` | 文本 |
| `openrouter` | `openrouter/auto`, `openai/gpt-4o` | 文本 / 文本+图片 |
| `ollama` | `llama3.2`, `llama3.2-vision` | 文本 / 文本+图片 |
| `lmstudio` / `vllm` / `llamacpp` | `local-model`（自行替换） | 默认文本 |

## 3. 配置方式

### 3.1 填写密钥

```powershell
Copy-Item .env.example .env
notepad .env
```

```dotenv
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=sk-...
# 其余留空即可
```

重启服务。`GET /v1/providers` 只显示 `configured` 与环境变量**名称**，不会返回值。

### 3.2 启用本地厂商

本地 provider 默认 `enabled: false`，避免在未安装时产生无意义的重试/降级。启用方式：

```powershell
$env:OMNI_ENABLE_LOCAL = "1"
python -m omninexus
```

或只启用一个：

```yaml
providers:
  ollama:
    enabled: true
```

Ollama 示例：

```powershell
ollama pull llama3.2
ollama serve
```

确保模型名与 `config/default.yaml` 中 `model` 一致；LM Studio 需开启本地 Server；vLLM/llama.cpp
需暴露 OpenAI-compatible `/v1` 接口。

### 3.3 新增一个 OpenAI-compatible 厂商

无需改代码：

```yaml
providers:
  myvendor:
    type: openai
    label: My Vendor
    base_url: https://api.example.com/v1
    api_key_env: MYVENDOR_API_KEY

models:
  - id: myvendor:chat
    provider: myvendor
    model: chat-2026
    label: My Vendor Chat
    input_modalities: [text, image]
    context_window: 128000
    fallbacks: [openai:gpt-4o-mini]
```

```dotenv
MYVENDOR_API_KEY=...
```

`id` 必须以 `provider:` 开头，`fallbacks` 必须是注册表中已存在的 ID。

### 3.4 特殊头部

OpenRouter 默认带 `HTTP-Referer` / `X-Title`，可在 `providers.openrouter.headers` 修改。其他厂商若
需要版本头/自定义头，也可在 provider 的 `headers` 中填写（**不要把密钥写在这里**）。

## 4. 能力与多模态

每个模型的能力在 `models[].input_modalities` 中声明：

```yaml
input_modalities: [text, image]
```

- 只有声明了 `image` 的模型才会收到图片。
- 声明错误会导致：图片被发给不支持的模型（厂商报错→回退）或本可视觉却被降级（质量损失）。
- 默认注册表按常见公开能力填写，**不保证**与你的账户/部署一致；可逐项修正。
- 音频/文件/视频默认不支持原生输入，按 [API 契约](api.md#13-图片) 的 metadata 文本化规则降级。

## 5. 回退链

```yaml
models:
  - id: openai:gpt-4o-mini
    fallbacks: [openai:gpt-4o, deepseek:deepseek-chat]
```

- 顺序：主模型 → 自身 fallbacks（深度优先）→ `gateway.fallback_models`。
- 未配置密钥的候选会被跳过；整条链不可用但存在其他已配置模型时，网关会重路由并标记
  `requested_unavailable`。
- 视觉输入会优先在同链中寻找支持视觉的候选；否则降级。
- 建议：至少配置一个**不同厂商**的文本回退，避免单点故障；视觉场景再配置一个视觉回退。

## 6. 安全说明

- 不要把密钥写进 `config/*.yaml`、提示词、Dockerfile、Compose、测试或提交记录。
- 不要把 `.env` 提交到 Git；仓库已忽略。
- 日志只打印 provider/model/错误码/耗时；错误正文不会透传给客户端。
- 使用第三方中转/OpenRouter 时，对话会发送给该中转及其路由到的模型厂商；请自行评估数据政策。
- 本地 provider 不经过云端，但模型本身可能仍有网络行为，需自行审查。
