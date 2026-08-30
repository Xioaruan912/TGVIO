import json
import os
import tempfile
import unittest
from pathlib import Path

from src.progress import position_token, render_bar
from src.storage import JsonStore


class HelperTests(unittest.TestCase):
    def test_progress_helpers(self):
        self.assertEqual(render_bar(50), "█████░░░░░")
        self.assertEqual(position_token(1), "①")
        self.assertEqual(position_token(10), "10")

    def test_json_store_round_trip_and_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            store = JsonStore(path, {})
            store.save({"ok": True})
            self.assertEqual(store.load(), {"ok": True})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"ok": True})

    def test_json_store_falls_back_on_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("not json")
            self.assertEqual(JsonStore(path, {"fallback": 1}).load(), {"fallback": 1})

    def test_docker_context_excludes_runtime_secrets(self):
        project_root = Path(__file__).resolve().parents[1]
        patterns = {
            line.strip()
            for line in (project_root / ".dockerignore").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertTrue({".env", "session", "downloads", ".git"} <= patterns)


if __name__ == "__main__":
    unittest.main()
