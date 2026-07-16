"""Tests that keep the Claude packaging honest inside the no-install CI.

The plugin manifest, marketplace listing, project MCP config, and every
shipped skill must stay parseable and internally consistent — a broken
manifest fails silently at install time otherwise.
"""

import json
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(relpath: str) -> dict:
    return json.loads((_ROOT / relpath).read_text(encoding="utf-8"))


class TestPluginManifest(unittest.TestCase):
    def test_plugin_json_parses_with_required_fields(self):
        p = _load(".claude-plugin/plugin.json")
        self.assertTrue(re.fullmatch(r"[a-z0-9][a-z0-9-]*", p["name"]))
        for field in ("description", "version", "license"):
            self.assertTrue(p.get(field), f"plugin.json missing {field}")

    def test_declared_skills_path_exists(self):
        p = _load(".claude-plugin/plugin.json")
        skills_dir = _ROOT / p["skills"]
        self.assertTrue(skills_dir.is_dir(), f"{p['skills']} does not exist")
        self.assertTrue(any(skills_dir.iterdir()), "skills dir is empty")

    def test_marketplace_lists_this_plugin(self):
        m = _load(".claude-plugin/marketplace.json")
        names = [pl["name"] for pl in m["plugins"]]
        self.assertIn(_load(".claude-plugin/plugin.json")["name"], names)

    def test_mcp_json_has_strava_server(self):
        cfg = _load(".mcp.json")
        strava = cfg["mcpServers"]["strava"]
        self.assertEqual(strava["type"], "http")
        self.assertTrue(strava["url"].startswith("https://"))
        # No secrets belong in a committed MCP config
        blob = json.dumps(cfg).lower()
        for word in ("token", "secret", "key", "password"):
            self.assertNotIn(word, blob)


class TestSkills(unittest.TestCase):
    def _skill_dirs(self):
        skills = sorted((_ROOT / ".claude" / "skills").iterdir())
        self.assertGreaterEqual(len(skills), 4)
        return skills

    def test_every_skill_has_frontmatter_description(self):
        for d in self._skill_dirs():
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), f"{d.name}: no frontmatter")
            frontmatter = text.split("---", 2)[1]
            m = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)
            self.assertTrue(m and len(m.group(1).strip()) > 40,
                            f"{d.name}: missing/thin description")
            m_name = re.search(r"^name:\s*(\S+)$", frontmatter, re.MULTILINE)
            self.assertEqual(m_name.group(1), d.name,
                             f"{d.name}: frontmatter name mismatch")

    def test_skills_reference_real_cli_commands(self):
        # Guard against skills drifting from the actual coach.py surface.
        coach_doc = (_ROOT / "coach.py").read_text(encoding="utf-8")
        for d in self._skill_dirs():
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            for cmd in re.findall(r"coach\.py (\w+)", text):
                self.assertIn(f"coach.py {cmd}", coach_doc,
                              f"{d.name} references unknown command '{cmd}'")


if __name__ == "__main__":
    unittest.main()
