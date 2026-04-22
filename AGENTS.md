# Agent 使用说明

这个仓库是一个通用的中文文案 humanize skill，不绑定单一 agent。

CoPaw / OpenClaw 这类 agent 的使用方式本质相同：读取 `SKILL.md`，在 skill 目录里执行 CLI。仓库里的 `scripts/install_to_copaw.py` 只是把文件同步到当前已知的 CoPaw workspace 路径，不代表 CoPaw 使用另一套协议。

适用环境：

- CoPaw
- OpenClaw
- Claude Code
- Hermes
- 任何能读取 `SKILL.md` 并执行本地 shell 命令的 agent

## 规范入口

默认只调用这个命令：

```bash
python3 humanize.py --text "{完整用户请求}" --output-root ./runs
```

如果在 skill 目录外执行，先进入仓库根目录：

```bash
cd /path/to/humanize
python3 humanize.py --text "{完整用户请求}" --output-root ./runs
```

## 不要这样做

- 不要手写 `challenger.txt` 来绕过官方流程。
- 不要自己主观挑 winner。
- 不要传 `--mode rewrite`，rewrite 会自动从 `--text` 或 `--original` 推断。
- 不要把用户的长 `原文` 丢掉，只传一个总结后的 `--task`。
- 不要在命令成功后再附加一版手工改写。

## 输出规则

如果命令输出：

```text
=== HUMANIZE_FINAL_RESPONSE_BEGIN ===
...
=== HUMANIZE_FINAL_RESPONSE_END ===
```

最终回复用户时，只返回两个 marker 中间的 markdown。

## 生成模型

默认优先使用可检测到的宿主 active model；当前仓库已内置 CoPaw active model 桥接。

如果当前 agent 没有提供可检测的 active model，可以配置本地 OpenAI-compatible endpoint：

```bash
export HUMANIZE_GENERATION_BACKEND=local
export HUMANIZE_LLM_BASE_URL=http://127.0.0.1:54841/v1
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
