"""The compatibility overlay must fail closed when upstream metadata changes."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.patch_hermes_dependencies import apply, MANIFEST


class HermesDependencyPatchTests(unittest.TestCase):
    def test_reviewed_override_preserves_other_dependencies_and_records_source_hashes(self):
        original = b'[project]\ndependencies = ["PyJWT[crypto]==2.13.0", "requests==2.33.0"]\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pyproject.toml"
            source.write_bytes(original)
            manifest = apply(root)
            self.assertIn(b'"PyJWT[crypto]==2.15.1"', source.read_bytes())
            self.assertIn(b'"requests==2.33.0"', source.read_bytes())
            self.assertEqual(manifest["before_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(manifest["after_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(json.loads((root / MANIFEST).read_text()), manifest)
            self.assertEqual(manifest["patch_sha256"], hashlib.sha256((root / manifest["patch_file"]).read_bytes()).hexdigest())
            script = Path(__file__).resolve().parents[1] / "scripts/patch_hermes_dependencies.py"
            self.assertEqual(manifest["patch_script_sha256"], hashlib.sha256(script.read_bytes()).hexdigest())

    def test_unexpected_or_duplicate_upstream_pins_are_not_modified(self):
        for declarations in ('"PyJWT[crypto]==2.14.0"', '"PyJWT[crypto]==2.13.0", "PyJWT[crypto]==2.13.0"'):
            with self.subTest(declarations=declarations), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "pyproject.toml"
                original = '[project]\ndependencies = [' + declarations + ']\n'
                source.write_text(original)
                with self.assertRaises(ValueError):
                    apply(root)
                self.assertEqual(source.read_text(), original)
                self.assertFalse((root / MANIFEST).exists())
