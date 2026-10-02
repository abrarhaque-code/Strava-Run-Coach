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

    def test_plugin_version_matches_the_changelog(self):
        version = _load(".claude-plugin/plugin.json")["version"]
        changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}]", changelog)

    def test_plugin_declares_the_same_strava_server(self):
        plugin = _load(".claude-plugin/plugin.json")
        mcp = _load(".mcp.json")
        self.assertEqual(plugin.get("mcpServers"), mcp["mcpServers"])
        blob = json.dumps(plugin).lower()
        for word in ("token", "secret", "password"):
            self.assertNotIn(word, blob)

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
        self.assertGreaterEqual(len(skills), 5)
        return skills

    def test_the_five_skills_ship(self):
        names = {d.name for d in self._skill_dirs()}
        for want in ("strava-coach-analyze", "review-run", "weekly-review", "race-forecast", "build-plan"):
            self.assertIn(want, names)

    def test_every_skill_points_at_the_coaching_notes(self):
        self.assertTrue((_ROOT / "docs" / "COACHING.md").exists())
        for d in self._skill_dirs():
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("COACHING.md", text, f"{d.name} does not point at docs/COACHING.md")

    def test_entry_skill_uses_the_plugin_data_home(self):
        text = (_ROOT / ".claude" / "skills" / "strava-coach-analyze" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('STRAVA_COACH_HOME="${CLAUDE_PLUGIN_DATA}"', text)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/coach.py", text)
        self.assertIn("status --json", text)
        self.assertIn("init --from-mcp", text)

    def test_coaching_notes_carry_the_core_rules(self):
        text = (_ROOT / "docs" / "COACHING.md").read_text(encoding="utf-8")
        for needle in ("Verify before you escalate", "HR is cost", "adjust and move forward",
                       "42-day", "three consecutive days", "pace-matched"):
            self.assertIn(needle, text)

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




class TestReadme(unittest.TestCase):
    """The README is the first thing a runner sees: its links must resolve and
    its top half must read as made for runners, not developers."""

    def _text(self):
        return (_ROOT / "README.md").read_text(encoding="utf-8")

    def test_relative_links_and_images_resolve(self):
        text = self._text()
        targets = re.findall(r"\]\(([^)\s#]+)(?:#[^)]*)?\)", text) + re.findall(r'src="([^"]+)"', text)
        self.assertTrue(targets)
        for t in targets:
            if t.startswith(("http://", "https://", "mailto:")):
                continue
            self.assertTrue((_ROOT / t).exists(), f"README links to a missing path: {t}")

    def test_no_developer_jargon_above_the_fold(self):
        fold = self._text().split("<details>", 1)[0]
        for word in ("stdlib", "MCP", "subprocess", "VDOT", "CTL", "OAuth", "gitignored", "clone"):
            self.assertNotIn(word, fold, f"{word!r} appears in the README above the fold")
        self.assertIn("coach me", fold)
        self.assertIn("docs/img/hero-conversation.png", fold)

    def test_terminal_guide_exists_and_is_linked(self):
        self.assertTrue((_ROOT / "docs" / "TERMINAL.md").exists())
        self.assertIn("docs/TERMINAL.md", self._text())


if __name__ == "__main__":
    unittest.main()
