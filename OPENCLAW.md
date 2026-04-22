# OpenClaw 使用说明

这个仓库可以直接在 OpenClaw 中使用。

当用户要求使用 `humanize` 优化中文文案时，执行：

```bash
python3 humanize.py --text "{完整用户请求}" --output-root ./runs
```

如果是第一次使用，可以先运行：

```bash
python3 scripts/bootstrap_runtime.py
```

注意：

- 保留用户完整输入，尤其是 `原文`、`原稿`、`正文` 后面的长文本。
- 不要传 `--mode rewrite`。
- 不要手写 challenger。
- 不要自己总结替代官方输出。
- 如果输出中有 `=== HUMANIZE_FINAL_RESPONSE_BEGIN ===` 和 `=== HUMANIZE_FINAL_RESPONSE_END ===`，最终只返回 marker 中间的 markdown。

如果当前环境没有可检测的宿主 active model，可以配置本地 OpenAI-compatible endpoint：

```bash
export HUMANIZE_GENERATION_BACKEND=local
export HUMANIZE_LLM_BASE_URL=http://127.0.0.1:11434/v1
export HUMANIZE_LLM_MODEL=<your-local-model-id>
```

如果你用的是 Ollama 上的 thinking 模型，建议再加：

```bash
export HUMANIZE_LLM_REASONING_EFFORT=none
```

如果在 Apple 芯片机器上想避免 scorer 首次走 MPS 带来的等待，也可以固定：

```bash
export HUMANIZE_SCORER_DEVICE=cpu
```

没有生成模型时，系统会降级到 `heuristic-only`，常见模板化文案仍可跑完整流程。
