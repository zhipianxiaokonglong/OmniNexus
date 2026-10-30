# 开发文档

## 1. 目录

```text
OmniNexus/
├─ config/default.yaml          # 非敏感配置、厂商/模型注册表
├─ src/omninexus/
│  ├─ api/                      # 路由、schema、SSE
│  ├─ core/                     # 领域模型、分类器、Token 估算
│  ├─ gateway/                  # 网关 + 厂商适配器
│  ├─ memory/                   # Reserved 记忆插件与冻结契约
│  ├─ providers/                # 自定义提供方、加密、测试、统一 catalog
│  ├─ prompts/                  # 提示词档案/版本
│  ├─ search/                   # 搜索源与 RAG
│  ├─ sessions/                 # 会话、上下文、导出
│  ├─ storage/                  # SQLite
│  ├─ static/                   # 无构建步骤的页面
│  ├─ config.py                 # YAML + env + 注册表
│  ├─ orchestrator.py           # 回合编排
│  ├─ logging.py                # 脱敏日志
│  └─ app.py                    # FastAPI 工厂
├─ tests/                       # pytest + MockTransport
├─ docs/                        # 开发/用户文档
├─ .github/workflows/ci.yml
├─ Dockerfile / docker-compose.yml
└─ pyproject.toml
```

## 2. 本地开发

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check src tests
python -m pytest --cov=omninexus --cov-report=term-missing
```

运行：

```powershell
$env:OMNI_ENABLE_ECHO = "1"
python -m omninexus --reload
```

`--reload` 会设置 `OMNI_CONFIG` 为当前配置路径，供 uvicorn 子进程使用。

## 3. 添加一个 OpenAI-compatible 厂商

通常只改 YAML，不改代码：

```yaml
providers:
  myvendor:
    type: openai
    label: My Vendor
    base_url: https://api.example.com/v1
    api_key_env: MYVENDOR_API_KEY
    headers: {}
    options: {}

models:
  - id: myvendor:chat
    provider: myvendor
    model: chat-v1
    label: My Chat
    input_modalities: [text, image]
    context_window: 128000
    max_output_tokens: 2048
    fallbacks: [openai:gpt-4o-mini]
```

约束：

- `model.id` 形如 `provider:model`，前缀必须等于 `provider`。
- `fallbacks` 必须引用已存在模型；不允许环（运行时深度限制 8）。
- `api_key_env` 只填变量名，不填值。
- 本地/无鉴权 provider 设 `requires_key: false`，并显式 `enabled: true`（默认样例是 false）。

## 4. 添加原生协议适配器

1. 在 `gateway/` 新建 `BaseProvider` 子类，实现：

```python
async def stream(...) -> AsyncIterator[ChatDelta]:
    ...
```

2. 在 `gateway/router.py::_PROVIDER_TYPES` 注册 `type_name`。
3. `ProviderConfig.type` 增加 Literal 值。
4. 用 `httpx.MockTransport` 写单元测试：SSE 解析、图片编码、usage、401/429/超时。
5. 更新 `docs/providers.md` 与契约测试。

适配器要求：

- 不记录请求/响应正文；错误使用 `ProviderError`（含 `retryable`）。
- 流式解析必须忽略未知事件/空行；SSE `[DONE]` 安全退出。
- 不把 API Key 拼进 URL（Gemini 使用 header）。
- 图片仅发送给声明支持视觉的模型（网关已过滤，但适配器也应防御）。

## 5. 添加 API

- 路由集中在 `api/routes.py`，请求模型放 `api/schemas.py`。
- 对外错误使用 `OmniError`/`NotFoundError`；禁止抛裸 `HTTPException` 携带内部信息。
- 写接口必须使用 `ConfigDict(extra="forbid")`。
- 对会话/提示词 ID 先校验存在，再写。
- 变更下游事件字段 = 契约变更，必须更新 `docs/api.md` 与测试。

## 6. 数据库变更

当前 schema version = 1（`storage/database.py::SCHEMA_VERSION`）。

新增字段/表：

1. 改 `_SCHEMA` 的 `CREATE TABLE IF NOT EXISTS`。
2. 增加 `schema_meta.version` 迁移函数；不要只改 schema 假设旧库自动升级。
3. 更新 `_row_to_*`、导出与测试。
4. 严禁在迁移中删除对话数据；需要时先导出。

`Database.transaction(fn)` 提供 `BEGIN IMMEDIATE`；异步代码不要在事务函数里 `await`。

## 7. 提示词安全

- 使用 `SandboxedEnvironment`，不加载模板文件。
- 变量只接受 JSON 标量；长度受限。
- 未定义变量渲染为空，避免运行时中断；语法错误在保存时拒绝。
- 不要把用户输入拼进系统提示的“可信指令”区域；检索内容用独立块包裹。

## 8. 保留记忆接口

- 契约文件：`memory/contract.py`，版本 `1.0`。
- 默认 `memory.enabled: false`；禁止改为默认启用。
- 不得删除 `HttpMemoryPlugin`、`DisabledMemoryPlugin`、契约测试或文档。
- 修改契约需联合评审 + 版本升级；见 [memory-reserved.md](memory-reserved.md)。

## 8.1 自定义提供方开发

- 新表/字段：`storage/database.py`（当前 schema v2）；会话新增 provider 字段是加法迁移。
- 密钥：`providers/secrets.py::SecretBox`；禁止把明文写入 DB/日志/响应。
- 存储/校验：`providers/store.py`；URL 规则：`providers/urls.py`（严格拼接，不补 `/v1`）。
- 统一目录：`providers/service.py`；网关动态适配器：`gateway/router.py::refresh_custom_providers`。
- 测试：`tests/test_custom_providers.py` 使用 `httpx.MockTransport`，覆盖 CRUD、加密、三级测试、
  模型发现、严格 URL、默认/删除/禁用、导入导出、迁移、流式聊天。

## 9. 测试约定

- 所有厂商网络调用用 `httpx.MockTransport`；CI 不调用真实 API。
- 测试不得依赖真实密钥；假密钥只用于验证脱敏。
- 故障注入优先覆盖：5xx、超时、429、空流、部分流、记忆超时、搜索失败。
- 每个修复应补回归测试；提交前跑：

```powershell
python -m pytest -q
python -m ruff check src tests
```

## 10. 发布清单（必须逐项）

- [ ] `pytest` 全绿，`ruff` 全绿。
- [ ] 无真实密钥：`git grep` 检查 `sk-`、`AIza`、`Bearer`、`api_key=`。
- [ ] 无 `.env`；只有 `.env.example` 且为空/占位。
- [ ] 无 `data/`、`*.db`、`*.log`、`*.jsonl`、`provider-secrets.key`、导出文件、截图临时文件。
- [ ] 无测试人格/临时人设：搜索 `persona`、`测试人格`、`test persona`、自定义临时配置名；
      `DELETE` 测试配置并清理 `data/`。
- [ ] 无调试输出/临时脚本/`print` 调试；测试脚本保留在 `tests/`。
- [ ] 日志不输出密钥/完整对话（运行脱敏测试）。
- [ ] 文档与默认配置一致（模型数、默认禁用项、Reserved 标注）。
- [ ] `src/omninexus/resources/default.yaml` 与 `config/default.yaml` 一致。
- [ ] Docker 构建成功；容器内只发布回环端口；数据卷可写。
- [ ] 版本号、README、CHANGELOG（如有）一致。
- [ ] GitHub 仓库默认分支、CI、License、README 徽章正确。

## 11. 代码风格

- Ruff：`E,F,I,UP,B,SIM,ASYNC`，行宽 100。
- 类型标注优先；公开函数写明契约/失败行为。
- 不记录敏感内容；日志使用 `get_logger(...)`。
- 配置默认值围绕“本地、安全、默认关闭增强能力”。
- 不在仓库中放生成物（构建、缓存、日志、数据库）。

## 12. 提交规范建议

```text
feat(gateway): add cohere streaming adapter
fix(api): encode non-ASCII export filenames
docs(memory): freeze reserved contract v1.0
test(degradation): cover memory timeout zero-impact
```

一次提交聚焦一件事；契约变更单独提交并在说明中标注版本。
