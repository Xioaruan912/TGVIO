"""Behavior checks for the cross-agent repository gates."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("repository_hygiene", SCRIPTS / "repository_hygiene.py")
hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hygiene)


class RepositoryHygieneTests(unittest.TestCase):
    def write(self, root, name, content):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_generated_outputs_cannot_be_tracked(self):
        for path in ("player/web/.test-dist/core.js", "player/web/dist/index.html", ".superpowers/trace.json", "src/__pycache__/x.pyc"):
            with self.subTest(path=path):
                self.assertTrue(hygiene.generated_path(path))
        self.assertFalse(hygiene.generated_path("src/tgvio/domain/job.py"))

    def test_new_modules_fail_above_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.write(root, "player/web/src/new.ts", "\n" * 601)
            self.assertIn("exceeds 600", hygiene.frontend_problems(root)[0])

    def test_existing_debt_cannot_grow(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.write(root, "player/web/src/main.ts", "\n" * 1710)
            self.assertEqual([], hygiene.frontend_problems(root))
            self.write(root, "player/web/src/main.ts", "\n" * 1711)
            self.assertIn("exceeds 1710", hygiene.frontend_problems(root)[0])

    def test_node_test_rejects_direct_typescript_import(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.write(root, "player/web/tests/x.test.mjs", 'import {x} from "../src/x.ts";')
            self.assertIn("compiled JS", hygiene.frontend_problems(root)[0])
            self.write(root, "player/web/tests/x.test.mjs", 'import {x} from "../.test-dist/x.js";')
            self.assertEqual([], hygiene.frontend_problems(root))

    def test_current_docs_reject_missing_and_untracked_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.write(root, "docs/development/README.md", "[target](other.md)")
            names = ["docs/development/README.md"]
            self.assertIn("missing", hygiene.document_problems(root, names)[0])
            self.write(root, "docs/development/other.md", "# present")
            self.assertIn("not tracked", hygiene.document_problems(root, names)[0])
            self.assertEqual([], hygiene.document_problems(root, names + ["docs/development/other.md"]))

    def test_history_links_are_not_current_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.write(root, "docs/refactor-v2/evidence/old.md", "[old](gone.md)")
            self.assertEqual([], hygiene.document_problems(root, ["docs/refactor-v2/evidence/old.md"]))
