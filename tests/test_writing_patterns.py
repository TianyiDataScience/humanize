from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from local_generation import build_direct_repair_prompt  # noqa: E402
from strategy_state import default_state, extract_failure_tags, state_directives  # noqa: E402
from writing_patterns import audit_writing_patterns, category_ids  # noqa: E402


def load_scoring_core_without_runtime_dependencies() -> types.ModuleType:
    module_name = "scoring_core_pattern_audit_test"
    replacements = {
        "torch": types.ModuleType("torch"),
        "yaml": types.ModuleType("yaml"),
        "transformers": types.ModuleType("transformers"),
    }
    replacements["yaml"].safe_load = lambda value: {}
    replacements["transformers"].AutoModelForSequenceClassification = object
    replacements["transformers"].AutoTokenizer = object
    previous = {name: sys.modules.get(name) for name in replacements}
    sys.modules.update(replacements)
    try:
        module_path = ROOT / "scripts" / "scoring_core.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise AssertionError("could not load scoring_core for the integration test")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(module_name, None)
        for name, original in previous.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def load_run_from_brief_without_runtime_dependencies() -> types.ModuleType:
    module_name = "run_from_brief_quality_gate_test"
    replacements = {
        "yaml": types.ModuleType("yaml"),
        "runtime_common": types.ModuleType("runtime_common"),
        "local_generation": types.ModuleType("local_generation"),
        "parse_user_brief": types.ModuleType("parse_user_brief"),
        "prepare_run": types.ModuleType("prepare_run"),
        "render_run_report": types.ModuleType("render_run_report"),
        "scoring_core": types.ModuleType("scoring_core"),
        "strategy_state": types.ModuleType("strategy_state"),
        "writing_patterns": types.ModuleType("writing_patterns"),
    }
    replacements["runtime_common"].reexec_into_runtime = lambda: None
    for name in (
        "build_direct_repair_prompt",
        "build_direct_rewrite_prompt",
        "build_generation_prompts",
        "call_chat",
        "discover_generation_backend",
        "extract_content",
    ):
        setattr(replacements["local_generation"], name, lambda *args, **kwargs: None)
    replacements["parse_user_brief"].build_payload = lambda *args, **kwargs: None
    replacements["prepare_run"].create_run_dir = lambda *args, **kwargs: None
    replacements["render_run_report"].build_html = lambda *args, **kwargs: None
    replacements["render_run_report"].build_markdown = lambda *args, **kwargs: None
    replacements["scoring_core"].DEFAULT_TEMPLATE_PHRASES = []
    replacements["scoring_core"].dump_score_json = lambda *args, **kwargs: None
    replacements["scoring_core"].score_candidate = lambda *args, **kwargs: None
    for name in (
        "choose_profiles",
        "evolve_after_attempts",
        "extract_failure_tags",
        "load_state",
        "save_state",
        "snapshot_state",
        "state_directives",
    ):
        setattr(replacements["strategy_state"], name, lambda *args, **kwargs: None)
    replacements["writing_patterns"].CATEGORY_LABELS = {"inflated_significance": "label"}

    previous = {name: sys.modules.get(name) for name in replacements}
    sys.modules.update(replacements)
    try:
        module_path = ROOT / "scripts" / "run_from_brief.py"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise AssertionError("could not load run_from_brief for the quality-gate test")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(module_name, None)
        for name, original in previous.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class WritingPatternAuditTests(unittest.TestCase):
    def test_single_signal_stays_visible_without_penalty(self) -> None:
        audit = audit_writing_patterns("本次发布标志着一个重要里程碑。")

        self.assertEqual(category_ids(audit), ["inflated_significance"])
        self.assertEqual(audit["score"], 1.0)
        self.assertFalse(audit["needs_repair"])

    def test_cluster_requests_repair_and_exposes_categories(self) -> None:
        text = "业内人士认为，这次发布标志着一个重要里程碑，也带来了令人惊艳的体验。未来值得期待。"
        audit = audit_writing_patterns(text)

        self.assertLess(audit["score"], 1.0)
        self.assertTrue(audit["needs_repair"])
        self.assertEqual(
            category_ids(audit),
            [
                "inflated_significance",
                "promotional_language",
                "vague_attribution",
                "generic_conclusion",
            ],
        )

    def test_quotes_and_normal_professional_copy_are_not_flagged(self) -> None:
        quoted = audit_writing_patterns("报道引用了“未来值得期待”，但未对结论作出评价。")
        professional = audit_writing_patterns("张教授在发布会上介绍了测试方法，团队将于周五公布结果。")

        self.assertEqual(quoted["categories"], [])
        self.assertEqual(professional["categories"], [])

    def test_cluster_becomes_specific_repair_guidance(self) -> None:
        text = "业内人士认为，这次发布标志着一个重要里程碑，也带来了令人惊艳的体验。未来值得期待。"
        audit = audit_writing_patterns(text)
        payload = {
            "notes": [],
            "writing_pattern_audit": audit,
            "hard_fail": False,
            "char_count": len(text),
        }
        tags = extract_failure_tags("", text, payload)
        directives = state_directives(default_state(), tags)

        self.assertIn("writing_patterns", tags)
        self.assertIn("writing_pattern:inflated_significance", tags)
        self.assertTrue(any("普通进展写成里程碑" in item for item in directives))

        _, prompt = build_direct_repair_prompt(
            task="优化一条产品介绍",
            hard_constraints={},
            source_text=text,
            current_best_text=text,
            failure_tags=tags,
            strategy_directives=directives,
        )
        self.assertIn("本轮修复要求：", prompt)
        self.assertIn("普通进展写成里程碑", prompt)

    def test_quality_gate_retry_keeps_valid_writing_pattern_categories(self) -> None:
        run_from_brief = load_run_from_brief_without_runtime_dependencies()
        tags = [
            "writing_patterns",
            "writing_pattern:inflated_significance",
            "writing_pattern:unknown",
            "too_similar",
            "writing_pattern:inflated_significance",
        ]
        expected = ["writing_patterns", "writing_pattern:inflated_significance", "too_similar"]

        self.assertEqual(run_from_brief.quality_gate_tags({"failure_tags": tags}), expected)
        self.assertEqual(run_from_brief.retryable_quality_tags(tags), expected)

    def test_scoring_payload_includes_the_audit_without_loading_a_model(self) -> None:
        scoring_core = load_scoring_core_without_runtime_dependencies()
        scoring_core.model_score = lambda query, candidate: 0.5
        text = "业内人士认为，这次发布标志着一个重要里程碑，也带来了令人惊艳的体验。未来值得期待。"

        payload = scoring_core.score_candidate(
            {"task": "优化一段产品介绍", "hard_constraints": {}},
            text,
        ).as_dict()

        self.assertLess(payload["rule_breakdown"]["writing_patterns"], 1.0)
        self.assertTrue(payload["writing_pattern_audit"]["needs_repair"])
        self.assertTrue(any(note.startswith("writing pattern audit:") for note in payload["notes"]))


if __name__ == "__main__":
    unittest.main()
