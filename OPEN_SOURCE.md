# 开源模型与依赖说明

这个项目的目标是：用户可以在本地安装、使用，并且不需要绑定闭源 API。

## 模型清单

| 用途 | 默认选择 | 来源 | 许可证 | 是否随仓库打包 |
| --- | --- | --- | --- | --- |
| 本地评分模型 | `BAAI/bge-reranker-v2-m3` | [Hugging Face](https://huggingface.co/BAAI/bge-reranker-v2-m3) | Apache-2.0 | 否，首次 bootstrap 自动下载 |
| 文案生成模型 | 可检测到的宿主 active model 或本地 OpenAI-compatible endpoint | 由用户选择 | 取决于用户选择的模型 | 否 |

## 重要说明

`humanize` 自己不会把闭源大模型打包进仓库，也不会强制调用某个云模型。

生成候选文本时，它默认优先调用可检测到的宿主 active model。当前仓库已经内置 CoPaw active model 桥接；其他 agent 可以通过本地 OpenAI-compatible endpoint 接入自己的模型。也就是说：

- 如果你在 CoPaw 里选择的是本地开源模型，那么这条链路就是本地 / 开源模型链路。
- 如果你在 CoPaw 里选择的是云 API 模型，那么生成侧会跟随你的 CoPaw 配置使用该模型。
- 如果你在 OpenClaw、Claude Code 或普通终端里使用，可以配置本地 OpenAI-compatible endpoint。

为了完全开源、尽量离线使用，建议在宿主的模型管理页面选择本地开源模型。以 CoPaw 为例，可以在「模型」页面选择本地模型，或者使用 CoPaw CLI 下载本地模型：

```bash
copaw models download Qwen/Qwen2-0.5B-Instruct-GGUF --source modelscope
copaw models set-llm
```

上面的模型只是 CoPaw 官方 CLI 帮助里给出的示例之一。`Qwen/Qwen2-0.5B-Instruct-GGUF` 在 Hugging Face 上标注为 Apache-2.0。实际使用时，请根据机器配置选择合适的本地开源模型。

CoPaw 本身是开源项目，官网标注 Apache-2.0，并支持本地 LLM。参考：[CoPaw 官网](https://copaw.bot/)。

非 CoPaw 环境可以这样指定本地生成模型：

```bash
export HUMANIZE_GENERATION_BACKEND=local
export HUMANIZE_LLM_BASE_URL=http://127.0.0.1:54841/v1
export HUMANIZE_LLM_MODEL=<your-local-model-id>
```

## 没有生成模型时会怎样

如果没有检测到可用的宿主 active model 或本地 OpenAI-compatible endpoint，`humanize` 不会在入口处直接失败。

它会降级到：

```text
heuristic-only
```

这意味着：

- 对社群通知、客服回复、产品宣传文案、常见 AI 套话长文案等已覆盖的场景，仍然可以跑完整流程。
- 对非常开放、复杂、没有模板特征的新任务，建议先配置 CoPaw 本地模型，以获得更好的泛化能力。

## Python 依赖

核心依赖来自常见开源生态：

- `torch`
- `transformers`
- `huggingface-hub`
- `PyYAML`
- `safetensors`

这些依赖不会 vendoring 到仓库里，而是在 `scripts/bootstrap_runtime.py` 创建的独立 venv 中安装。

## 本地目录

模型和 runtime 默认存放在：

```text
${COPAW_WORKING_DIR:-~/.copaw}/models/humanize/
```

仓库 `.gitignore` 已经排除了：

- `runs/`
- `logs/`
- `models/`
- `.copaw/`
- Python cache

避免把本地模型、运行报告、日志和机器状态误传到开源仓库。
