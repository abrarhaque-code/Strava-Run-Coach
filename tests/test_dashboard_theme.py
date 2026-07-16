"""Tests for the dashboard theme plumbing.

Every colour must flow through config.theme() -> _theme_vars(): no hardcoded
ink rgba() literals and none of the retired pre-design-system hexes may
survive in the emitted CSS. _theme_vars/_build_css are pure functions of the
theme dict, so this needs no data files.
"""

import unittest

import config
from dashboard import _build_css, _theme_vars

# Palette values retired by the Yves Klein Blue design-system reskin.
RETIRED_HEXES = ("#002FA7", "#0047FF", "#E5341F", "#F7F6F1", "#FFFEFB",
                 "#F1F0EA", "#B9C6EE", "#0A0A0A")


class TestThemeVars(unittest.TestCase):
    def test_all_vars_emitted(self):
        css = _theme_vars(config.theme())
        for var in ("--klein:", "--paper-lt:", "--ink:", "--ink-2:", "--ink-3:",
                    "--hair:", "--hair-inv:", "--verm:", "--tint:", "--c1:",
                    "--c5:", "--rule-lt:var(--hair)"):
            self.assertIn(var, css)

    def test_example_theme_is_design_system(self):
        css = _theme_vars(config.theme())
        self.assertIn("#1D1DE6", css)   # --ikb-500
        self.assertIn("#F0400C", css)   # --flame-500
        self.assertIn("#0B0B8A", css)   # heatmap top = --ikb-900

    def test_old_configs_without_new_keys_still_render(self):
        legacy = dict(config.theme())
        for k in ("ink_soft", "ink_faint", "hairline"):
            legacy.pop(k, None)
        css = _theme_vars(legacy)
        self.assertIn("--ink-2:#5C5C5E", css)   # token defaults kick in
        self.assertIn("--hair:#D9D5C7", css)


class TestBuildCss(unittest.TestCase):
    def test_no_hardcoded_ink_rgba(self):
        css = _build_css(config.theme())
        self.assertNotIn("rgba(10,10,10", css)

    def test_no_retired_palette_hexes(self):
        css = _build_css(config.theme())
        for hexval in RETIRED_HEXES:
            self.assertNotIn(hexval.lower(), css.lower())

    def test_fonts_come_from_theme(self):
        css = _build_css(config.theme())
        self.assertIn(config.theme()["display_font"], css)
        self.assertIn(config.theme()["mono_font"], css)


if __name__ == "__main__":
    unittest.main()
