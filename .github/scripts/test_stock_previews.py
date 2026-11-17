import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

import validate_catalog as catalog


class StockPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = io.BytesIO()
        Image.new("RGB", (862, 1920), "purple").save(image, "WEBP")
        cls.content = image.getvalue()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        (self.folder / "previews").mkdir()
        self.index = {"schemaVersion": 1, "previews": []}
        for path in sorted(catalog.STOCK_PREVIEW_PATHS):
            scene = Path(path).stem.rsplit("-", 2)[0]
            self.index["previews"].append({"path": path, "role": catalog.PREVIEW_ROLES.get(scene, "other"),
                "sha256": hashlib.sha256(self.content).hexdigest(), "caption": scene})
            (self.folder / path).write_bytes(self.content)
        self.write_index()

    def write_index(self):
        (self.folder / "index.json").write_text(json.dumps(self.index), encoding="utf-8")

    def test_complete_stock_gallery(self):
        self.assertEqual(catalog.validate_stock_previews(self.folder), self.index)

    def test_rejects_bad_contracts(self):
        original = copy.deepcopy(self.index)
        cases = [
            lambda d: d.update(schemaVersion=True),
            lambda d: d.update(appearance={}),
            lambda d: d["previews"].pop(),
            lambda d: d["previews"].__setitem__(1, d["previews"][0]),
            lambda d: d["previews"][0].update(path="../escape.webp"),
            lambda d: d["previews"][0].update(role="player"),
            lambda d: d["previews"][0].update(sha256="A" * 64),
            lambda d: d["previews"][0].update(caption="x" * 161),
            lambda d: d["previews"][0].update(extra=True),
        ]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                self.index = copy.deepcopy(original)
                mutate(self.index)
                self.write_index()
                with self.assertRaises(ValueError):
                    catalog.validate_stock_previews(self.folder)

    def test_rejects_modified_image(self):
        (self.folder / self.index["previews"][0]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            catalog.validate_stock_previews(self.folder)

    def test_rejects_wrong_size_even_with_matching_hash(self):
        content = io.BytesIO()
        Image.new("RGB", (8, 8)).save(content, "WEBP")
        entry = self.index["previews"][0]
        (self.folder / entry["path"]).write_bytes(content.getvalue())
        entry["sha256"] = hashlib.sha256(content.getvalue()).hexdigest()
        self.write_index()
        with self.assertRaisesRegex(ValueError, "862x1920"):
            catalog.validate_stock_previews(self.folder)

    def test_rejects_extra_assets(self):
        (self.folder / "appearance.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "unexpected Stock assets"):
            catalog.validate_stock_previews(self.folder)

    def test_rejects_duplicate_keys_and_oversized_index(self):
        path = self.folder / "index.json"
        path.write_text('{"schemaVersion":1,"schemaVersion":1,"previews":[]}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON member"):
            catalog.validate_stock_previews(self.folder)
        path.write_bytes(b" " * (catalog.STOCK_INDEX_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            catalog.validate_stock_previews(self.folder)


if __name__ == "__main__":
    unittest.main()
