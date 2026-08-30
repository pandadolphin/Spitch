"""Pure behavior tests for the live preedit overlay."""

import unittest

from spitch.ui.preedit import format_elapsed, overlay_position, preview_text


class FormattingTests(unittest.TestCase):
    def test_elapsed(self):
        self.assertEqual(format_elapsed(-1), "0:00")
        self.assertEqual(format_elapsed(2.9), "0:02")
        self.assertEqual(format_elapsed(65), "1:05")

    def test_preview_keeps_latest_text(self):
        self.assertEqual(preview_text("  你好   世界  "), "你好 世界")
        self.assertEqual(preview_text("abcdefgh", limit=5), "…efgh")


class PositionTests(unittest.TestCase):
    def test_bottom_center(self):
        self.assertEqual(
            overlay_position((220, 40), (0, 0, 800, 600)),
            (290, 488),
        )

    def test_respects_offset_workarea(self):
        self.assertEqual(
            overlay_position((220, 40), (1920, 24, 1920, 1056)),
            (2770, 968),
        )

    def test_clamps_when_overlay_is_larger_than_workarea(self):
        self.assertEqual(
            overlay_position((900, 700), (10, 20, 800, 600)),
            (10, 20),
        )


if __name__ == "__main__":
    unittest.main()
