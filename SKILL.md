---
name: humanize
description: Use this skill when the user wants to generate or optimize Chinese communication copy so it sounds more human, more natural, less templated, and less like polished AI writing. 中文文案去 AI 味和人味优化 skill，适合自媒体文案、客户邮件、微信回复、售后沟通、面试跟进、上级汇报、产品宣传和社群通知。The user normally only needs to provide the task and constraints. If they also provide an original draft, the skill switches to rewrite mode automatically.
metadata:
  version: "0.1.3"
  copaw:
    emoji: "📝"
    requires:
      bins:
        - python3
---

> Important: all `scripts/` paths are relative to this skill directory.
> Preferred entrypoint: `cd {this_skill_dir} && python3 humanize.py --text "{entire_user_request}" --output-root ./runs`
> Lower-level scripts still work via: `cd {this_skill_dir} && python3 scripts/...`
> The runtime and model cache live under `${COPAW_WORKING_DIR:-~/.copaw}/models/humanize/`.
> In agent shell calls, prefer one-line commands. Do not paste backslash-continued multi-line commands. Always set the shell tool timeout to at least `120` seconds for `python3 humanize.py`.
> Final relay rule: if `python3 humanize.py ...` prints `=== HUMANIZE_FINAL_RESPONSE_BEGIN ===` / `=== HUMANIZE_FINAL_RESPONSE_END ===`, return exactly the markdown between them as the final answer. Do not summarize it or add any explanation.
> Invocation rule: do not build helper JSON or temporary Python snippets to call this skill. Invoke `python3 humanize.py` directly.
> Forbidden invocation: do not call `copaw skills run humanize`, `python -m skills.humanize...`, or any package-style wrapper. Do not pass `--mode`; rewrite mode is inferred automatically from the full `--text` request or `--original`. They are not the canonical entrypoint for this skill.
> Preservation rule: pass the user's full request verbatim via `--text` by default. If the request contains `原文`, `原稿`, `正文`, `draft`, or a long draft body, never reinterpret it into separate `--task` / `--constraints` arguments and never drop the original draft.
> Compatibility rule: this skill is not CoPaw-only. CoPaw / OpenClaw style agents should use the same `SKILL.md + python3 humanize.py --text ...` flow. The CoPaw installer is only a convenience sync script for the currently known CoPaw workspace path, not a separate skill protocol. Claude Code and other local coding agents can invoke the same CLI after reading this `SKILL.md`.

# Humanize

## What This Skill Does

This skill is a practical AutoResearch-style loop for one narrow job:
optimize Chinese communication copy until it reads more like something a real
person would send.

The user normally only needs to define:

- `task`: what situation this message is for
- `constraints`: hard limits such as length, phrases to keep, or phrases to avoid

Optional:

- `original draft`: only when the user wants rewrite mode instead of generate mode

This skill then:

1. Bootstraps a local runtime and downloads the default local scorer model
2. Normalizes the user's input into a spec and session mode
3. Creates a run folder with the spec and drafts
4. Generates a baseline when the user did not provide one
5. Generates multiple challenger drafts with different profiles
6. Scores each candidate locally with the official scorer
7. Uses failure tags to repair the next round if nothing improved
8. Persists a small strategy state so the next run starts from the better policy bias
9. Records each round in JSON and renders a visible report so the process is inspectable

## First Run

Before the first evaluation, bootstrap the local runtime:

```bash
cd {this_skill_dir} && python3 scripts/bootstrap_runtime.py
```

This installs a dedicated venv under CoPaw's working directory and downloads
the default scorer model:

- `BAAI/bge-reranker-v2-m3`

## Inputs You Need From The User

Always collect or infer these before you start iterating:

- `task`
- `hard_constraints.min_chars` / `hard_constraints.max_chars` when length matters
- `hard_constraints.must_include` when facts must be preserved
- `hard_constraints.banned_phrases` for phrases the user dislikes

Optional:

- `original draft`

Default assumptions for V1:

- `goal`: built in, unless the user explicitly overrides it
- `rounds`: `1` full visible comparison round
- `style_notes`: infer from the task and constraints unless the user adds special tone requirements
- `session_mode`: `generate` unless the user provides an original draft

If the user does not explicitly give a spec file, create one in the run folder.
If the user does not provide a custom `goal`, use the built-in default goal internally:

```text
更像真人自然发送的中文沟通消息，减少模板腔、客服腔、公告腔和过度AI润色感。保持清楚、可信、有分寸。
```

## Product Semantics

This skill follows one product rule:

- the user only needs to express intent and necessary boundaries

Split inputs into two layers.

### Task Semantics: infer automatically

These are part of the task itself, not extra user constraints.
Infer them automatically from the user's intent.

Examples:

- `给催进度客户发邮件回复` means:
  - output should look like an email reply, not a one-line chat message
  - recipient is a customer
  - tone should be professional, natural, and clear
  - structure should usually include greeting + current progress + next step / time point

- `给催进度客户发微信回复` means:
  - output should look like a short WeChat-style message
  - recipient is a customer
  - tone should be natural, concise, and human

- `给老板汇报进度` means:
  - recipient is a manager
  - tone should be direct, stable, and not too casual

Do not ask the user to restate these semantics as constraints.
If the task already says `邮件`, `微信`, `客户`, `上级`, `面试官`, or similar, the skill should understand the output form and default tone on its own.

### User Constraints: only when explicitly stated

Only treat something as a hard constraint if the user clearly says it.

Examples:

- `保留“明天下午”和“财务”`
- `控制在 90 字内`
- `不要出现“感谢您的耐心等待”`
- `不要太像模板回复`
- `更强势一点`
- `不要承诺今天回复`

If the user did not state a constraint, do not invent one.
No hidden hard limits. No forced formatting rules beyond what the task semantics already imply.

### Priority Order

When these layers interact, apply them in this order:

1. user explicit constraints
2. task semantic defaults
3. built-in humanize defaults

Example:

- task = `给催进度客户发邮件回复`
- constraint = `控制在 60 字内`

Then the output should still try to behave like an email reply, but the explicit length limit wins.

## Default Behavior

When this skill is triggered, treat the following as the default workflow.
The user should **not** need to restate these execution steps each time.

Unless the user explicitly asks for a different mode, always:

1. Normalize the user's message with `scripts/prepare_run.py`
2. If `session_mode = generate`, generate exactly one baseline draft from task + constraints
3. If `session_mode = rewrite`, treat the user's original message as `baseline`
4. Generate multiple challenger drafts with different profiles
5. If the first challenger set does not improve, run one repair retry round using the failure tags
6. Run the full visible session
7. Show the user:
   - baseline text
   - every round's candidate texts
   - final challenger text
   - baseline score
   - every candidate's score
   - failure tags
   - selected candidate per round
   - challenger score
   - delta
   - keep/discard decision
   - session trace
   - a human-readable process summary
   - report.html path

These are default responsibilities of the skill.
Do not ask the user to additionally request the full process.
When the skill runs, always reveal the optimization process by default.
After the official run finishes, do not append a second manual rewrite that overrides the skill result.
Relay the official humanize output first. Only provide an extra manual suggestion if the user explicitly asks for another variant.

For normal CoPaw execution, call the skill with the raw user request:

```bash
cd {this_skill_dir} && python3 humanize.py --text "{entire_user_request}" --output-root ./runs
```

Do not summarize the user's request into `--task` and `--constraints` unless the user did not provide an original draft and the request is already a simple generation task. If there is any `原文` / draft content, `--text` is mandatory so the parser can switch to rewrite mode itself.

If the user only says something like "generate a more human customer reply",
you should still follow the full visible session flow automatically.

## Non-Negotiable Rules

- Do **not** replace the official local scorer with ad-hoc rule-only scoring just because `torch` or `transformers` look complex.
- `scripts/score_copy.py`, `scripts/compare_candidates.py`, and `scripts/run_session.py` automatically re-enter the managed runtime. Use them as-is.
- If the runtime is missing, run `python3 scripts/bootstrap_runtime.py` once. If scoring still fails, report the failure. Do not invent a fake keep/discard result.
- If `run_from_brief.py` succeeds and prints `HUMANIZE_USER_VISIBLE_SUMMARY`, treat that as the canonical result. Do not overwrite it with a separate handcrafted rewrite or a conflicting final answer.
- If the tool output contains `=== HUMANIZE_FINAL_RESPONSE_BEGIN ===` and `=== HUMANIZE_FINAL_RESPONSE_END ===`, your final user-facing reply must be exactly the markdown between those markers. Do not summarize it, do not paraphrase it, and do not add another conclusion after it.
- After relaying that block, stop immediately. Do not append praise, explanation, bullets, follow-up questions, or any extra sentence after the block.
- For normal use, do **not** switch into a debug flow with `prepare_run.py` + handwritten `challenger.txt` + manual compare loops. That is only for debugging when the user explicitly asks for debugging.
- If `python3 humanize.py ...` times out, retry the same official command once with a longer timeout. Do not fall back to manual candidate writing, manual scoring, or subjective winner selection.
- Do not run a sequence like "prepare run -> handwrite several challengers -> compare them manually -> recommend a lower-scoring rewrite". That violates the skill's canonical flow.
- Do not generate ad-hoc `python -c` wrappers, JSON builders, or helper scripts just to pass the user's text into this skill. Use the top-level `python3 humanize.py` command directly.

## Default Workflow

### Preferred: Run One Full Visible Session

Preferred single-entry command:

```bash
cd {this_skill_dir} && python3 humanize.py --task "给催进度客户发微信回复" --constraints "保留“明天下午”和“财务”，控制在90字内" --output-root ./runs
```

This command:

- `spec.yaml`
- `source.txt`
- `parse-result.json`
- `baseline.txt`
- `challenger.txt`
- `baseline.generation.json` and `challenger.generation.json` when drafts are auto-generated
- `session-trace.json`
- `session-trace.md`
- `user-visible.md`
- `user-visible.html`
- `strategy-state.before.json`
- `strategy-state.after.json`
- `compare-result.json`
- `report.html`

Mode behavior:

- If the user provided an original draft, `run_from_brief.py` automatically uses the original draft as baseline and only auto-generates the challenger unless explicitly overridden.
- If the user did not provide an original draft, `run_from_brief.py` automatically generates both baseline and challenger unless explicitly overridden.
- The iteration budget is adaptive:
  - `rewrite + 邮件` defaults to `1` challenger round for responsiveness
  - most `rewrite` runs default to `2` challengers and `1` round
  - most `generate` runs default to `2` challengers and up to `2` rounds
- The retry round is driven by failure tags such as missing facts, wrong audience, too-short copy, or no improvement.
- The skill persists a lightweight strategy state under `${COPAW_WORKING_DIR:-~/.copaw}/models/humanize/strategy-state.json`.
- At the end, prefer showing `user_visible_summary_markdown` directly to the user instead of only listing file paths.
- `run_from_brief.py` now prints a human-readable process summary to stdout before the JSON payload. Prefer relaying that summary directly.
- When relaying results, do not collapse the run into a one-line summary. Show baseline, round-by-round candidates, scores, failure tags, selected candidate, and the final decision by default.
- `--baseline-text` and `--challenger-text` are optional override hooks for debugging, not the default UX.
- In rewrite mode, prefer `--original-draft`. Do not substitute it with `--baseline-text` during normal use.

If you need to inspect planning before running the score, prepare the run first.
This is a debug-only path, not the default user flow:

```bash
cd {this_skill_dir} && python3 scripts/prepare_run.py --text "用 humanize 帮我生成并优化一条中文沟通消息。任务：给催进度客户发微信回复。约束：保留“明天下午”和“财务”，控制在90字内。" --output-root ./runs
```

This creates a full run directory containing:

- `spec.yaml`
- `source.txt`
- `baseline.txt`
- `challenger.txt`
- `baseline.score.json`
- `challenger.score.json`
- `compare-result.json`
- `best.txt`
- `rounds.jsonl`
- `report.md`
- `report.html`

Use this flow whenever you want the entire optimization process to be visible.

## Minimal UX For The User

The preferred user input is only:

- the task / recipient context
- any hard constraints that matter

Optional:

- the original message, if the user wants rewrite mode

This means the default product UX is:

```text
用 humanize 帮我生成并优化一条中文沟通消息。

任务：给催进度客户发微信回复
约束：保留“明天下午”和“财务”，控制在 90 字内
```

Loose fallback input is also valid. For example:

```text
用 humanize 帮我生成一条更像真人发的客户微信，保留“明天下午”和“财务”，控制在90字内。
```

Rewrite mode is also valid. For example:

```text
用 humanize 帮我把这段话改得更像真人一点：
您好，这边已经和财务同事再次确认过了，预计明天下午会给到您明确反馈。感谢您的理解与支持，如有任何问题请随时联系我。
```

When fields are not labeled, infer:

- `task`: default to a generic Chinese communication optimization task
- `constraints`: only from obvious signals such as `90字内` or quoted keep/avoid phrases
- `original draft`: only when the remaining text looks like a real message body

The mode rule is:

- if the user provides an original draft: `rewrite`
- if the user only provides task + constraints: `generate`

Do not ask the user for a `goal` by default.
Only ask for it when the user explicitly wants a different direction from the
built-in "humanize this Chinese message" behavior.

Do **not** require the user to manually request:

- baseline vs challenger
- score comparison
- keep/discard
- run directory creation
- report.html output

Those are the skill's default responsibilities.

### Manual: Step By Step

#### 1. Create a run folder

```bash
cd {this_skill_dir} && python3 scripts/init_run.py --spec examples/demo_spec.yaml --source examples/demo_baseline.txt --output-root ./runs
```

This prints a run directory path such as `./runs/20260409-130000-demo`.

#### 2. Put the baseline draft into the run folder

Save the current best draft into `baseline.txt`.

#### 3. Score the baseline

```bash
cd {this_skill_dir} && python3 scripts/score_copy.py --spec ./runs/<run-id>/spec.yaml --candidate ./runs/<run-id>/baseline.txt --source ./runs/<run-id>/source.txt
```

#### 4. Generate a challenger

Write one revised draft into `challenger.txt`.

When revising, optimize for:

- more human and believable Chinese phrasing
- less template tone
- less service-script language
- less "perfectly polished AI" feel
- still preserving hard constraints and required facts

#### 5. Compare baseline vs challenger

```bash
cd {this_skill_dir} && python3 scripts/compare_candidates.py --spec ./runs/<run-id>/spec.yaml --baseline ./runs/<run-id>/baseline.txt --challenger ./runs/<run-id>/challenger.txt --source ./runs/<run-id>/source.txt
```

Interpret the result:

- `decision = keep`: promote challenger to best version
- `decision = discard`: keep the existing baseline

#### 6. Record the round

```bash
cd {this_skill_dir} && python3 scripts/record_round.py --run-dir ./runs/<run-id> --result ./runs/<run-id>/compare-result.json
```

Repeat only when the user explicitly asks for more than one round.

#### 7. Render a readable report

```bash
cd {this_skill_dir} && python3 scripts/render_run_report.py --run-dir ./runs/<run-id>
```

This generates `report.md` and `report.html`.

## What The Local Score Means

The exposed score is a single `final_score`, but internally it is a composite:

- `model_score`: how well the candidate matches the user's task + naturalness rubric
- `rule_score`: hard constraints, banned phrases, template-phrase penalties, formatting penalties

This is not an "AI detector" score. It is a "human-like Chinese communication
fit" score.

See:

- `references/scoring.md`
- `references/presets.md`

## Keep / Discard Rule

Default behavior:

- keep only if challenger passes hard constraints
- keep only if challenger beats baseline by at least the configured margin
- otherwise discard and try a different rewrite direction

## Best Practices

- Keep each challenger intentionally different; do not make tiny random edits
- Preserve required facts with `must_include`
- Do not overfit to one phrase-level rule; optimize overall believability
- Show the user the baseline score, challenger score, and keep/discard decision

## Files In This Skill

- `scripts/bootstrap_runtime.py`: first-run installer and model downloader
- `scripts/parse_user_brief.py`: normalize structured or loose user input into spec + session mode
- `scripts/prepare_run.py`: create a run directory from a raw user brief and decide `generate` vs `rewrite`
- `scripts/run_from_brief.py`: one-command wrapper that prepares the run and executes the official scorer
- `scripts/score_copy.py`: score one candidate
- `scripts/compare_candidates.py`: compare baseline vs challenger
- `scripts/create_spec.py`: create a spec file from simple task/constraint inputs, with goal optional
- `scripts/init_run.py`: create a run directory
- `scripts/record_round.py`: append round results to a log
- `scripts/run_session.py`: one-shot visible run with scores, decision, and report
- `scripts/render_run_report.py`: render a human-readable report
- `scripts/install_to_copaw.py`: copy this skill into local CoPaw and enable it
