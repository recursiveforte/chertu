import unittest

from chert.discord.markdown import discord_tables


class DiscordTableTests(unittest.TestCase):
    def test_screenshot_table_becomes_wrapping_labeled_rows(self):
        source = (
            "**7. Scale through explicit milestones.**\n\n"
            "| Milestone | Required result |\n|---|---|\n"
            "| Reproducible foundation | Restore missing artifacts |\n"
            "| Leaf proof | Actual C-to-machine theorem for one simple function |\n\n"
            "I would use simple load/store functions."
        )
        self.assertEqual(discord_tables(source), (
            "**7. Scale through explicit milestones.**\n\n"
            "- **Milestone**: Reproducible foundation\n"
            "  **Required result**: Restore missing artifacts\n\n"
            "- **Milestone**: Leaf proof\n"
            "  **Required result**: Actual C-to-machine theorem for one simple function\n\n"
            "I would use simple load/store functions."
        ))

    def test_optional_outer_pipes_alignment_and_inline_formatting(self):
        source = (
            "Name | Value | Notes\n:--- | :---: | ---:\n"
            "**Bold** | `left|right` | [link](https://example.com) and a\\|b\n"
            "| next | ``a`|b`` | |\n"
        )
        rendered = discord_tables(source)
        self.assertIn("**Name**: **Bold**", rendered)
        self.assertIn("**Value**: `left|right`", rendered)
        self.assertIn("[link](https://example.com) and a\\|b", rendered)
        self.assertIn("**Value**: ``a`|b``", rendered)

    def test_fenced_and_indented_examples_and_prose_stay_verbatim(self):
        table = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
        for source in (
            "Use a | b in a pipeline.\n", "`a|b`\n---|---\nx|y\n",
            "```md\n" + table + "```\n",
            "~~~~md\n" + table + "~~~\n" + table + "~~~~\n",
            "```md\n" + table,
            "".join("    " + line for line in table.splitlines(keepends=True)),
            "A | B\n--- | nope\n1 | 2\n",
            "A | B\n--- | ---\n", "A | B\n--- | ---\n1 | 2 | 3\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(discord_tables(source), source)

    def test_multiple_tables_and_fence_closure(self):
        source = "```\nexample\n```\nA | B\n--- | ---\nx | y\n\nC | D\n--- | ---\nz | q"
        rendered = discord_tables(source)
        self.assertIn("```\nexample\n```\n- **A**: x", rendered)
        self.assertTrue(rendered.endswith("**D**: q"))
        self.assertEqual(discord_tables(rendered), rendered)
