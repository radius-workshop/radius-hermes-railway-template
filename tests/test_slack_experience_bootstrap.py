"""Exercise the entrypoint's Slack experience env -> config.yaml bridge."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = (ROOT / "scripts/entrypoint.sh").read_text()
SECTION = ENTRYPOINT.split("Applying Slack experience settings", 1)[1]
BRIDGE = SECTION.split("<<'PYEOF'", 1)[1].split("\n", 1)[1].split("\nPYEOF", 1)[0]

SLACK_VARS = (
    "SLACK_STREAMING", "SLACK_LIVE_STATUS", "SLACK_RUNTIME_FOOTER", "SLACK_RICH_BLOCKS",
    "SLACK_NATIVE_TASK_CARDS", "SLACK_FEEDBACK_BUTTONS", "SLACK_SUGGESTED_PROMPTS",
    "SLACK_CHANNEL_PROMPTS", "SLACK_CHANNEL_SKILL_BINDINGS", "HERMES_TOOL_PROGRESS_MODE",
)


class SlackExperienceBootstrapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.config = self.home / "config.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def boot(self, **env_vars):
        env = {k: v for k, v in os.environ.items() if k not in SLACK_VARS}
        env.update(HERMES_HOME=str(self.home), **env_vars)
        result = subprocess.run([sys.executable, "-c", BRIDGE], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def load(self):
        return yaml.safe_load(self.config.read_text()) or {}

    def test_unset_vars_leave_existing_config_untouched(self):
        original = "model: keep-me\nslack:\n  channel_prompts:\n    C1: operator prompt\n"
        self.config.write_text(original)
        self.boot()
        self.assertEqual(self.config.read_text(), original)

    def test_each_var_writes_its_hermes_key(self):
        self.config.write_text(yaml.safe_dump({
            "model": "keep-me",
            "display": {"platforms": {"slack": {"tool_progress": "new"}}},
            "slack": {"reply_in_thread": True},
        }))
        self.boot(
            SLACK_STREAMING="true", SLACK_LIVE_STATUS="verb", SLACK_RUNTIME_FOOTER="on",
            SLACK_RICH_BLOCKS="true", SLACK_NATIVE_TASK_CARDS="yes", SLACK_FEEDBACK_BUTTONS="true",
            SLACK_SUGGESTED_PROMPTS="default",
            SLACK_CHANNEL_PROMPTS='{"C0123": "Support triage mode."}',
            SLACK_CHANNEL_SKILL_BINDINGS='[{"id": "C0123", "skills": ["x402-payments"]}]',
        )
        cfg = self.load()
        display = cfg["display"]["platforms"]["slack"]
        self.assertEqual(display["streaming"], True)
        self.assertEqual(display["live_status"], "verb")
        self.assertEqual(display["runtime_footer"], {"enabled": True, "fields": ["model", "context_pct"]})
        self.assertEqual(display["tool_progress"], "new")
        slack = cfg["slack"]
        self.assertIs(slack["rich_blocks"], True)
        self.assertIs(slack["native_task_cards"], True)
        self.assertIs(slack["feedback_buttons"], True)
        self.assertEqual(len(slack["suggested_prompts"]), 4)
        self.assertTrue(all(len(p["title"]) <= 75 for p in slack["suggested_prompts"]))
        self.assertEqual(slack["channel_prompts"], {"C0123": "Support triage mode."})
        self.assertEqual(slack["channel_skill_bindings"], [{"id": "C0123", "skills": ["x402-payments"]}])
        self.assertIs(slack["reply_in_thread"], True)
        self.assertEqual(cfg["model"], "keep-me")
        self.assertNotIn("streaming", cfg)  # global streaming (other platforms) untouched

    def test_gating_keys_are_never_written(self):
        self.boot(SLACK_RICH_BLOCKS="true")
        slack = self.load()["slack"]
        for key in ("require_mention", "strict_mention", "allow_bots", "allowed_channels", "reaction_triggers"):
            self.assertNotIn(key, slack)

    def test_invalid_value_warns_and_does_not_block_other_keys(self):
        self.config.write_text(yaml.safe_dump({"slack": {"channel_prompts": {"C9": "old"}}}))
        result = self.boot(
            SLACK_CHANNEL_PROMPTS="{not json", SLACK_LIVE_STATUS="loud", SLACK_RICH_BLOCKS="true",
        )
        cfg = self.load()
        self.assertEqual(cfg["slack"]["channel_prompts"], {"C9": "old"})
        self.assertIs(cfg["slack"]["rich_blocks"], True)
        self.assertNotIn("display", cfg)
        self.assertIn("Ignoring SLACK_CHANNEL_PROMPTS", result.stderr)
        self.assertIn("Ignoring SLACK_LIVE_STATUS", result.stderr)

    def test_removing_a_var_reverts_only_keys_this_bridge_wrote(self):
        self.config.write_text(yaml.safe_dump({"slack": {"channel_prompts": {"C1": "operator"}}}))
        self.boot(SLACK_RICH_BLOCKS="true", SLACK_STREAMING="true")
        self.boot(SLACK_STREAMING="true")
        cfg = self.load()
        self.assertNotIn("rich_blocks", cfg["slack"])
        self.assertEqual(cfg["slack"]["channel_prompts"], {"C1": "operator"})
        self.assertIs(cfg["display"]["platforms"]["slack"]["streaming"], True)
        self.boot()
        self.assertNotIn("display", self.load())

    def test_none_clears_a_key(self):
        self.config.write_text(yaml.safe_dump({"slack": {"suggested_prompts": [{"title": "a", "message": "b"}]}}))
        self.boot(SLACK_SUGGESTED_PROMPTS="none")
        self.assertNotIn("slack", self.load())

    def test_prompt_text_is_not_logged(self):
        result = self.boot(SLACK_CHANNEL_PROMPTS='{"C0123": "secret-ish internal instructions"}')
        self.assertNotIn("secret-ish", result.stdout + result.stderr)
        self.assertIn("C0123", result.stdout)

    def test_footer_fields_are_validated(self):
        self.boot(SLACK_RUNTIME_FOOTER="model,latency")
        footer = self.load()["display"]["platforms"]["slack"]["runtime_footer"]
        self.assertEqual(footer, {"enabled": True, "fields": ["model", "latency"]})
        result = self.boot(SLACK_RUNTIME_FOOTER="model,colour")
        self.assertIn("unknown fields", result.stderr)
        self.assertEqual(self.load()["display"]["platforms"]["slack"]["runtime_footer"], footer)

    def test_dependency_warnings(self):
        result = self.boot(SLACK_FEEDBACK_BUTTONS="true", SLACK_NATIVE_TASK_CARDS="true", HERMES_TOOL_PROGRESS_MODE="off")
        self.assertIn("feedback buttons need Block Kit", result.stderr)
        self.assertIn("task cards are off", result.stderr)
        result = self.boot(SLACK_FEEDBACK_BUTTONS="true", SLACK_RICH_BLOCKS="true", SLACK_NATIVE_TASK_CARDS="true")
        self.assertNotIn("WARNING", result.stderr)

    def test_unparseable_config_is_never_rewritten(self):
        broken = "model: keep-me\nterminal: [unclosed\n"
        self.config.write_text(broken)
        result = self.boot(SLACK_RICH_BLOCKS="true")
        self.assertEqual(self.config.read_text(), broken)
        self.assertIn("Could not parse", result.stderr)

    def test_invalid_value_on_later_boot_keeps_managed_value(self):
        self.boot(SLACK_CHANNEL_PROMPTS='{"C1": "first"}')
        self.boot(SLACK_CHANNEL_PROMPTS="{broken")
        self.assertEqual(self.load()["slack"]["channel_prompts"], {"C1": "first"})
        self.boot()
        self.assertNotIn("slack", self.load())

    def test_suggested_prompts_title_form_and_false_values(self):
        prompts = {"title": "Try:", "prompts": [{"title": "Balance", "message": "What is my SBC balance?"}]}
        self.boot(SLACK_SUGGESTED_PROMPTS=json.dumps(prompts), SLACK_STREAMING="false")
        cfg = self.load()
        self.assertEqual(cfg["slack"]["suggested_prompts"], prompts)
        self.assertIs(cfg["display"]["platforms"]["slack"]["streaming"], False)

    def test_skill_bindings_require_skill_names(self):
        result = self.boot(SLACK_CHANNEL_SKILL_BINDINGS='[{"id": "C1", "skills": 5}]')
        self.assertIn("Ignoring SLACK_CHANNEL_SKILL_BINDINGS", result.stderr)
        self.boot(SLACK_CHANNEL_SKILL_BINDINGS='[{"id": "C1", "skill": "x402-payments"}]')
        self.assertEqual(self.load()["slack"]["channel_skill_bindings"], [{"id": "C1", "skill": "x402-payments"}])

    def test_task_card_warning_follows_hermes_precedence(self):
        self.config.write_text(yaml.safe_dump({"display": {"tool_progress": "off", "platforms": {"slack": {"tool_progress": "new"}}}}))
        result = self.boot(SLACK_NATIVE_TASK_CARDS="true")
        self.assertNotIn("task cards are off", result.stderr)
        self.config.write_text(yaml.safe_dump({"display": {"tool_progress_overrides": {"slack": "off"}}}))
        result = self.boot(SLACK_NATIVE_TASK_CARDS="true")
        self.assertIn("task cards are off", result.stderr)

    def test_block_kit_replies_add_a_slack_prompt_hint(self):
        result = self.boot(SLACK_RICH_BLOCKS="true")
        hint = self.load()["platform_hints"]["slack"]["append"]
        self.assertIn("pipe table", hint)
        self.assertIn("cannot send Block Kit JSON", hint)
        self.assertIn("slack-card", hint)
        self.assertIn("platform_hints.slack = Block Kit formatting note", result.stdout)
        self.assertNotIn(hint, result.stdout)
        # Slack's native markdown block wins over rich_blocks and has other limits: no note.
        self.config.write_text(yaml.safe_dump({"slack": {"markdown_blocks": True}}))
        (self.home / ".slack-experience-managed.json").unlink()
        self.boot(SLACK_RICH_BLOCKS="true")
        self.assertNotIn("platform_hints", self.load())

    def test_operator_edit_of_the_managed_hint_is_kept(self):
        self.boot(SLACK_RICH_BLOCKS="true")
        cfg = self.load()
        cfg["platform_hints"]["slack"]["append"] = "Operator rewrote this."
        self.config.write_text(yaml.safe_dump(cfg))
        result = self.boot(SLACK_RICH_BLOCKS="true")
        self.assertEqual(self.load()["platform_hints"]["slack"], {"append": "Operator rewrote this."})
        self.assertIn("Keeping the existing platform_hints.slack", result.stdout)
        self.boot()
        self.assertEqual(self.load()["platform_hints"]["slack"], {"append": "Operator rewrote this."})

    def test_block_kit_hint_is_removed_when_blocks_turn_off(self):
        self.config.write_text(yaml.safe_dump({"platform_hints": {"telegram": "keep"}}))
        self.boot(SLACK_RICH_BLOCKS="true")
        result = self.boot(SLACK_RICH_BLOCKS="false")
        self.assertEqual(self.load()["platform_hints"], {"telegram": "keep"})
        self.assertIn("removed (Block Kit replies are off)", result.stdout)
        self.boot()
        self.assertEqual(self.load(), {"platform_hints": {"telegram": "keep"}})

    def test_operator_slack_hint_is_never_replaced(self):
        operator = {"platform_hints": {"slack": {"replace": "Operator Slack hint."}}}
        self.config.write_text(yaml.safe_dump(operator))
        result = self.boot(SLACK_RICH_BLOCKS="true")
        self.assertEqual(self.load()["platform_hints"], operator["platform_hints"])
        self.assertIn("Keeping the existing platform_hints.slack", result.stdout)
        self.boot()
        self.assertEqual(self.load(), operator)

    def test_no_hint_without_block_kit(self):
        self.boot(SLACK_STREAMING="true", SLACK_FEEDBACK_BUTTONS="true")
        self.assertNotIn("platform_hints", self.load())

    def test_state_file_tracks_managed_vars(self):
        self.boot(SLACK_RICH_BLOCKS="false")
        self.assertEqual(json.loads((self.home / ".slack-experience-managed.json").read_text()), ["SLACK_RICH_BLOCKS"])
        self.assertIs(self.load()["slack"]["rich_blocks"], False)


if __name__ == "__main__":
    unittest.main()
