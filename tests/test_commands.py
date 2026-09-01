import unittest

from src.commands import COMMAND_SPECS, command_help_text, command_menu_pairs


class CommandPresentationTests(unittest.TestCase):
    def test_command_names_are_unique_and_menu_descriptions_are_compact(self) -> None:
        names = [spec.name for spec in COMMAND_SPECS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            names,
            [
                "start", "queue", "begin", "end", "mode", "profiles", "sources",
                "webdav", "webdavlogs", "proxy", "dashboard", "stats", "health", "diag", "about",
            ],
        )
        for name, description in command_menu_pairs():
            self.assertTrue(name)
            self.assertLessEqual(len(description), 32)

    def test_help_text_explains_every_command_once(self) -> None:
        text = command_help_text(include_start=True)
        for spec in COMMAND_SPECS:
            self.assertEqual(text.count(f"/{spec.name} —"), 1)
            self.assertIn(spec.help_text, text)

        home_text = command_help_text(include_start=False)
        self.assertNotIn("/start —", home_text)
        self.assertIn("/queue —", home_text)


if __name__ == "__main__":
    unittest.main()
