# Contributing

感谢参与 OmniNexus。提交前请阅读 [开发文档](docs/development.md) 与 [发布清单](docs/development.md#10-发布清单必须逐项)。

## 基本要求

- Python 3.11+，保留类型标注与文档字符串。
- `python -m ruff check src tests` 与 `python -m pytest -q` 必须通过。
- 新增厂商/模型优先通过 YAML 配置，不硬编码。
- 未经联合评审，不修改：
  - 下游 `OutputEvent` 字段；
  - 保留记忆 wire contract v1.0；
  - 标签枚举。
- 不提交密钥、`.env`、对话数据、日志、数据库、测试人格或临时脚本。
- 测试使用 `httpx.MockTransport`，不得在 CI 调用真实付费 API。

## 提交信息

建议 Conventional Commits：

```text
feat(gateway): ...
fix(api): ...
docs(prompt): ...
test(memory): ...
chore(release): ...
```

## 安全

发现密钥泄漏或安全问题，请先私下联系维护者，不要公开提交 issue 附带密钥或对话数据。参见
[隐私与安全](docs/privacy-security.md)。
