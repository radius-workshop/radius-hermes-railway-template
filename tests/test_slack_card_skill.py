"""Keep the bundled slack-card skill installable and consistent with the prompts that name it."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/slack-card.md"


class SlackCardSkillTests(unittest.TestCase):
    def setUp(self):
        text = SKILL.read_text(encoding="utf-8")
        _, front, self.body = text.split("---\n", 2)
        self.meta = yaml.safe_load(front)

    def test_frontmatter(self):
        self.assertEqual(self.meta["name"], SKILL.stem)
        for word in ("card", "themed", "Block Kit", "wallet"):
            self.assertIn(word, self.meta["description"])
        # Internal formatting help, not an offered capability: keep it off /.well-known.
        self.assertIsNot(self.meta.get("published"), True)

    def test_template_uses_markdown_the_renderer_supports_and_real_tools(self):
        # A fenced template gets copied with its fence and renders as a code block.
        self.assertNotRegex(self.body, r"(?m)^\s*(```|~~~)")
        template = re.search(r"Card starts:\n\n(## .*?)\n\nCard ends\.", self.body, re.S).group(1)
        self.assertNotIn("{", template)  # no JSON payloads
        self.assertNotIn("[<", template)  # a bracketed placeholder would break the link
        self.assertEqual(len(re.findall(r"^\|[-:| ]+\|$", template, re.M)), 1)  # one table
        self.assertNotRegex(template, r":[a-z_]+:")  # Unicode emoji, not shortcodes
        for tool in ("radius_wallet_address", "radius_balance"):
            self.assertIn(tool, self.body)

    def test_prompts_point_at_the_skill(self):
        self.assertIn("`slack-card` skill", (ROOT / "HERMES.md").read_text(encoding="utf-8"))
        self.assertIn("slack-card skill", (ROOT / "scripts/entrypoint.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
