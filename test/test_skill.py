"""Skill frontmatter checks: the description must survive YAML parsing."""
from __future__ import annotations

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "skills", "agent-relay", "SKILL.md")


def _frontmatter_lines():
    with open(SKILL, encoding="utf-8") as handle:
        text = handle.read()
    if not text.startswith("---\n"):
        raise AssertionError("SKILL.md must start with a frontmatter block")
    return text.split("---\n", 2)[1].splitlines()


class SkillFrontmatterTests(unittest.TestCase):
    def test_description_is_a_block_or_quoted_scalar(self):
        lines = _frontmatter_lines()
        index = next(i for i, line in enumerate(lines) if line.startswith("description:"))
        value = lines[index][len("description:"):].strip()
        self.assertTrue(
            value in (">-", ">", "|", "|-") or value.startswith('"') or value.startswith("'"),
            "description must be a block scalar or quoted, so a ': ' cannot break YAML",
        )
        if value in (">-", ">", "|", "|-"):
            for continuation in lines[index + 1:]:
                if not continuation.strip():
                    continue
                self.assertTrue(
                    continuation.startswith((" ", "\t")),
                    "block scalar continuation must be indented: %r" % continuation,
                )

    def test_description_parses_when_yaml_is_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        with open(SKILL, encoding="utf-8") as handle:
            text = handle.read()
        frontmatter = text.split("---\n", 2)[1]
        data = yaml.safe_load(frontmatter)
        self.assertIsInstance(data.get("description"), str)
        self.assertTrue(data["description"].strip())

    def test_description_names_the_harnesses_and_triggers(self):
        text = "\n".join(_frontmatter_lines()).lower()
        for keyword in ("pi", "codex", "opencode", "claude code", "delegate", "orchestrate"):
            self.assertIn(keyword, text)


if __name__ == "__main__":
    unittest.main()
