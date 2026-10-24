# 用户文档

## 1. 安装

### 1.1 前置条件

- Python 3.11 或更新版本（推荐 3.13）。
- 可访问所选厂商 API 的网络（本地模型不需要）。
- 一个或多家厂商的 API Key（BYOK；OmniNexus 不提供账户）。

### 1.2 安装步骤（Windows）

```powershell
git clone https://github.com/zhipianxiaokonglong/OmniNexus.git
cd OmniNexus
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
notepad .env
```

在 `.env` 中填入至少一个密钥：

```dotenv
OPENAI_API_KEY=sk-你的密钥
```

启动：

```powershell
python -m omninexus
```

浏览器打开 <http://127.0.0.1:8000>。

### 1.3 Linux / macOS

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
# 编辑 .env
python -m omninexus
```

### 1.4 不配置密钥的离线试用

```powershell
$env:OMNI_ENABLE_ECHO = "1"
python -m omninexus
```

在页面模型下拉框中选 **Offline diagnostic echo**。它只回显输入并提示未调用外部模型，用于确认
安装、页面、SSE 与标签链路；不是正式人设，也不代表真实模型质量。

## 2. 配置厂商

1. 复制 `.env.example` 为 `.env`。
2. 填写所需厂商的密钥。未填的厂商保持 `（未配置）`，不会被调用。
3. 重启服务。
4. 页面左下角「厂商与模型」可查看每个厂商是否已配置。

完整厂商、Base URL 与密钥变量名见 [厂商接入](providers.md)。

### 本地模型

本地 provider 默认禁用。启用全部：

```powershell
$env:OMNI_ENABLE_LOCAL = "1"
```

或只启用 `config/default.yaml` 中的某一个（如 `ollama.enabled: true`）。确认本地服务已启动，且
模型名与注册表一致：

```powershell
ollama pull llama3.2
ollama serve
```

## 3. 页面使用

### 3.1 新对话

点击左上「＋ 新对话」。第一次发送时自动创建会存档会话；标题取首条文本的前 60 字。

### 3.2 选择模型

顶部「模型」下拉框按厂商分组。`（未配置）` 项不可选。切换后：

- 会话模型持久化；
- 历史消息保留，下一轮以新模型继续；
- 若目标模型不支持历史中的图片，会自动降级为文字说明。

### 3.3 提示词

顶部「提示词」选择配置；左下「提示词管理」可：

- 查看/新建配置；
- 编辑模板并保存为新版本；
- 查看版本历史并激活旧版本（回滚）；
- 重置到初始版本；
- 删除自定义配置（内置 `default` 不可删除）。

模板变量、接口和正式人设填写流程见 [提示词接口](prompt-api.md)。

> 仓库默认系统提示词为空。正式人设由产品完成后统一填写，页面和 API 已准备好。

### 3.4 联网搜索

「联网搜索」开关默认禁用。启用前需要在 `config/default.yaml` 配置搜索源，并在 `.env` 配置密钥
（如 `TAVILY_API_KEY`）。

- 只发送**当前查询**，不发送历史；
- 回答末尾追加「参考来源」；
- 页面在回答下方显示可点击引用；
- 搜索失败不会中断回答，只是没有引用。

### 3.5 图片

点击输入框左侧图片按钮，选择 PNG/JPEG/WebP/GIF（≤8 MB）。

- 支持视觉的模型会直接接收图片；
- 不支持时，优先自动路由到视觉模型；没有可用视觉模型则以文字占位回答；
- 原始图片保存在本地存档，JSON 导出包含完整 data URL。

### 3.6 停止生成

生成中「发送」变为「停止」。点击后取消上游请求；已生成的部分应尽量保存，重新打开会话可见。
部分内容会标注中断。

### 3.7 导出 / 删除

右上角：

- `⇩` 导出 JSON（完整存档，含图片与引用）；
- `⌫` 删除会话（消息与引用级联删除，不可恢复）。

API 还支持 Markdown 导出：`GET /v1/sessions/{id}/export?format=md`。

## 4. 存档与数据位置

| 内容 | 位置 |
| --- | --- |
| 会话/消息/引用/提示词 | `data/omninexus.db` |
| WAL 临时文件 | `data/omninexus.db-wal`, `-shm` |
| 可选日志 | `data/`（若配置 `server.log_file`） |
| 密钥 | `.env`（仅本机，Git 忽略） |

备份：停止服务后复制整个 `data/`。迁移到另一台机器时同时迁移 `.env`（妥善保管）。

## 5. 保留扩展：外部记忆

本期**未接入**外部记忆管线，默认禁用。正式记忆来自本地存档 + 最近窗口。页面「厂商与模型」底部
会显示其状态为 `unavailable`。这不影响任何功能。

详见 [保留记忆接口](memory-reserved.md)。

## 6. 常见问题

| 现象 | 原因/处理 |
| --- | --- |
| 页面所有模型显示「未配置」 | `.env` 未填密钥或未重启；检查 `GET /v1/providers` |
| `503 NO_PROVIDER_AVAILABLE` | 无任何已配置模型；填密钥或 `OMNI_ENABLE_ECHO=1` |
| `502 PROVIDER_AUTH` | 密钥无效/过期/权限不足；检查厂商控制台 |
| `504 PROVIDER_TIMEOUT` | 网络/厂商慢；可增大 `gateway.first_token_timeout_s`，或配置回退 |
| 429 / 频繁切备用 | 厂商限流或本地 `rate_limit_rpm` 太小；调整配置 |
| 中文导出下载失败 | 已使用 RFC 5987；若旧版本出现，升级到当前代码 |
| 图片回答忽略内容 | 模型无视觉能力且无视觉回退；检查能力表/切换视觉模型 |
| 历史太长被截断 | 上下文窗口裁剪；旧消息仍在存档，可导出 |
| 搜索无结果 | 搜索源未启用/缺密钥/网络失败；查看 `GET /v1/search/status` |
| 页面打不开 | 服务是否启动；端口是否被占用；`OMNI_HOST/OMNI_PORT` |
| 局域网访问被拒 | `TrustedHostMiddleware` 默认只允许 localhost；改 `server.trusted_hosts` |
| 提示词保存 422 | 模板语法错误/超长/名称重复；用预览接口定位 |
| 重置后没有系统提示词 | 默认 version 1 为空；这是预期行为，请填写正式人设 |

### 日志

默认输出到控制台（INFO）。若需要文件：

```yaml
server:
  log_file: logs/omninexus.log
  log_format: json
```

日志被 Git 忽略；不要手工把日志提交到仓库。日志不含对话正文和密钥。

## 7. 页面无法加载时的 API 自检

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/health | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8000/v1/models | ConvertTo-Json -Depth 5
```

## 8. 升级

```powershell
git pull
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m pytest -q
python -m omninexus
```

SQLite schema 当前版本 1。升级前建议备份 `data/`。如未来引入迁移，会在 `schema_meta.version`
中记录。
