# Changelog

## 0.1.0 — 2026-10-24

首个可独立运行的交付版本（任务书 v1.1）。

### Added

- 多厂商模型网关：OpenAI-compatible（OpenAI、DeepSeek、Mistral、xAI、Qwen、Doubao、GLM、
  Moonshot、Qianfan、Spark、Hunyuan、MiniMax、OpenRouter、Ollama、LM Studio、vLLM、llama.cpp）、
  Anthropic、Gemini、Cohere 原生适配器。
- 运行时模型切换、上下文延续、重试、首 Token 超时、进程内限流、模型回退链与已配置模型重路由。
- 多模态能力协商：视觉路由、文本化降级、回退时图片剥离。
- 提示词档案与不可变版本：创建/保存/激活/重置/删除/预览；Jinja2 沙箱变量注入。
- 本地对话存档（SQLite WAL）：会话、消息、引用、导出 JSON/Markdown、删除、Token 预算裁剪。
- 确定性情感/内容类型/意图标签，随 `delta`/`final` 事件下发。
- 可配置联网搜索（Tavily/Brave/SearXNG/DuckDuckGo/自定义），RAG + 引用溯源，查询最小化。
- SSE 契约：`meta`/`route`/`delta`/`final`/`citations`/`warning`/`error` 与心跳。
- 本地零构建 Web 页面：对话、模型/提示词切换、图片、搜索、存档、导出/删除、提示词管理。
- 隐私：密钥仅环境变量、日志脱敏、错误正文不回显、`.env`/data/log 忽略。
- **Reserved**：外部记忆插件接口（默认禁用、健康探测 `unavailable`、契约 v1.0、mock 验证）。
- 文档：架构、API、提示词、厂商、降级、隐私、用户、开发、测试报告、验收对照。
- CI、Dockerfile、docker-compose、MIT License。

### Notes

- 未内置正式人设或测试人格；默认系统提示词为空。
- 外部记忆管线不参与本期交付。
- 不提供密钥/账户（BYOK）。
