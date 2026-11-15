import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from PIL import Image

import validate_catalog as catalog


class CatalogValidationTest(unittest.TestCase):
    @staticmethod
    def image_bytes(image_format):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), "purple").save(buffer, image_format)
        return buffer.getvalue()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.manifest = catalog.load_json(catalog.ROOT / "demo/examples/theme-v1/manifest.json")
        self.appearance = (catalog.ROOT / "demo/examples/theme-v1/appearance.json").read_bytes()

    def write_json(self, name, document):
        path = self.folder / name
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return path

    def validate_theme(self):
        return catalog.validate_theme(self.folder, catalog.load_schema("theme"), catalog.load_schema("appearance"))

    def write_theme(self, appearance=None):
        appearance = self.appearance if appearance is None else appearance
        (self.folder / "appearance.json").write_bytes(appearance)
        self.manifest["appearance"]["sha256"] = hashlib.sha256(appearance).hexdigest()
        self.write_json("manifest.json", self.manifest)

    def test_appearance_limit_includes_json_whitespace(self):
        at_limit = self.appearance.ljust(catalog.THEME_DOCUMENT_LIMIT, b" ")
        self.write_theme(at_limit)
        self.validate_theme()
        self.write_theme(at_limit + b" ")
        with self.assertRaisesRegex(ValueError, "exceeds 131072 bytes"):
            self.validate_theme()

    def test_preview_limit_is_checked_even_when_hash_matches(self):
        (self.folder / "previews").mkdir()
        for size in (catalog.PREVIEW_LIMIT, catalog.PREVIEW_LIMIT + 1):
            content = self.image_bytes("WEBP").ljust(size, b" ")
            (self.folder / "previews/player-normal.webp").write_bytes(content)
            self.manifest["previews"] = [{"path": "previews/player-normal.webp", "role": "player",
                                          "sha256": hashlib.sha256(content).hexdigest()}]
            self.write_theme()
            if size == catalog.PREVIEW_LIMIT:
                self.validate_theme()
            else:
                with self.assertRaisesRegex(ValueError, "exceeds 4194304 bytes"):
                    self.validate_theme()

    def test_custom_creator_media_uses_preview_limit(self):
        profile = catalog.load_json(catalog.ROOT / "demo/examples/creator-v1/profile.json")
        for size in (catalog.PREVIEW_LIMIT, catalog.PREVIEW_LIMIT + 1):
            content = self.image_bytes("WEBP").ljust(size, b" ")
            (self.folder / "banner.webp").write_bytes(content)
            profile["banner"] = {"source": "custom", "asset": {
                "path": "banner.webp", "sha256": hashlib.sha256(content).hexdigest()}}
            self.write_json("profile.json", profile)
            if size == catalog.PREVIEW_LIMIT:
                catalog.validate_creator(self.folder, catalog.load_schema("creator"))
            else:
                with self.assertRaisesRegex(ValueError, "exceeds 4194304 bytes"):
                    catalog.validate_creator(self.folder, catalog.load_schema("creator"))

    def test_images_must_decode_as_declared_even_with_matching_hash(self):
        for suffix, image_format in {".jpg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".gif": "GIF"}.items():
            path = self.folder / f"preview{suffix}"
            with self.subTest(suffix=suffix):
                for content, expected_to_decode in (
                    (self.image_bytes(image_format), True),
                    (self.image_bytes("JPEG" if suffix == ".png" else "PNG"), False),
                    (b"x", False),
                ):
                    path.write_bytes(content)
                    sha256 = hashlib.sha256(content).hexdigest()
                    if expected_to_decode:
                        catalog.validate_image(path, sha256)
                    else:
                        with self.assertRaisesRegex(ValueError, f"invalid {image_format}"):
                            catalog.validate_image(path, sha256)

    def test_review_logins_reject_trailing_line_break(self):
        schema = catalog.load_schema("reviews").schema
        login = schema["properties"]["reviews"]["items"]["properties"]["author"]["properties"]["login"]
        validator = Draft202012Validator(login)
        self.assertTrue(validator.is_valid("Review-User"))
        self.assertFalse(validator.is_valid("Review-User\n"))

    def test_navigation_layout_values_reject_trailing_line_break(self):
        schema = catalog.load_schema("appearance").schema
        navigation = schema["properties"]["categories"]["properties"]["navigation"]["properties"]
        dock_item = navigation["toolbars"]["properties"]["dockOrder"]["items"]
        for rule, value in ((dock_item, "home"), (navigation["searchGridLayouts"], "0101010")):
            validator = Draft202012Validator(rule)
            self.assertTrue(validator.is_valid(value))
            self.assertFalse(validator.is_valid(value + "\n"))

    def test_metadata_limit_applies_before_parsing(self):
        path = self.folder / "index.json"
        path.write_bytes(b"{}".ljust(catalog.CATALOG_LIMIT, b" "))
        self.assertEqual({}, catalog.load_json(path))
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "exceeds 524288 bytes"):
            catalog.load_json(path)

    def test_nonfinite_literals_are_rejected_before_schema_validation(self):
        path = self.folder / "appearance.json"
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                path.write_text('{"radius": ' + literal + '}', encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "not a JSON number"):
                    catalog.load_json(path)

    def test_catalog_json_requires_utf8_without_bom(self):
        path = self.folder / "index.json"
        text = '{"title": "Été"}'
        path.write_bytes(text.encode("utf-8"))
        self.assertEqual({"title": "Été"}, catalog.load_json(path))
        for encoding in ("utf-8-sig", "utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                path.write_bytes(text.encode(encoding))
                with self.assertRaisesRegex(ValueError, "UTF-8"):
                    catalog.load_json(path)

    def test_metadata_integers_require_integer_notation(self):
        path = self.folder / "index.json"
        for version in ("1", "1.0", "1e0", "true"):
            with self.subTest(version=version):
                path.write_text('{"schemaVersion": ' + version + ', "themes": []}', encoding="utf-8")
                if version == "1":
                    catalog.validate_json(path, catalog.load_schema("catalog"))
                else:
                    with self.assertRaisesRegex(ValueError, "integer notation"):
                        catalog.validate_json(path, catalog.load_schema("catalog"))

        for field, values in (
            ("revision", (1, 1.0)),
            ("author", (self.manifest["author"], {**self.manifest["author"], "githubId": 1.0})),
            ("compatibility", ({"minVersionCode": 96}, {"minVersionCode": 96.0})),
        ):
            for value, valid in zip(values, (True, False)):
                with self.subTest(field=field, value=value):
                    self.assertEqual(valid, catalog.load_schema("theme").is_valid({**self.manifest, field: value}))

    def test_appearance_still_accepts_equivalent_numeric_notation(self):
        path = self.folder / "appearance.json"
        validator = catalog.load_schema("appearance")
        for literal in ("1", "1.0", "1e0", "-2.147483648e9", "2147483647.0", "0e-9999"):
            with self.subTest(literal=literal):
                path.write_text(
                    '{"schemaVersion": 1.0, "categories": {"colors": {"mode": "system", '
                    '"palette": "dynamic", "customAccent": ' + literal + '}}}', encoding="utf-8",
                )
                catalog.validate_json(path, validator)

    def test_appearance_integer_tokens_are_checked_without_rounding(self):
        path = self.folder / "appearance.json"
        validator = catalog.load_schema("appearance")
        for literal in ("1.0000000000000001", "2147483647.00000001", "1e-9999"):
            with self.subTest(literal=literal):
                path.write_text(
                    '{"schemaVersion": 1, "categories": {"colors": {"mode": "system", '
                    '"palette": "dynamic", "customAccent": ' + literal + '}}}', encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "customAccent"):
                    catalog.validate_json(path, validator)
        path.write_text('{"schemaVersion": 1.0000000000000001, "categories": {}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "schemaVersion"):
            catalog.validate_json(path, validator)

    def test_duplicate_members_cannot_hide_invalid_metadata(self):
        path = self.folder / "index.json"
        path.write_text('{"schemaVersion": 1.0, "schemaVersion": 1, "themes": []}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON member 'schemaVersion'"):
            catalog.validate_json(path, catalog.load_schema("catalog"))

    def test_demo_versions_require_integer_notation(self):
        for name, sections in (("index.json", ("assets",)), ("visuals.json", ("covers", "banners"))):
            with self.subTest(name=name):
                path = self.write_json(name, {"schemaVersion": 1.0, **dict.fromkeys(sections, [])})
                with self.assertRaisesRegex(ValueError, "unsupported index version"):
                    catalog.load_demo_index(path, sections)

    def test_demo_indexes_use_android_byte_limit(self):
        for name, sections in (("index.json", ("assets",)), ("visuals.json", ("covers", "banners"))):
            with self.subTest(name=name):
                document = {"schemaVersion": 1, **dict.fromkeys(sections, [])}
                path = self.folder / name
                at_limit = json.dumps(document).encode("utf-8").ljust(8192, b" ")
                path.write_bytes(at_limit)
                self.assertEqual(document, catalog.load_demo_index(path, sections))
                path.write_bytes(at_limit + b" ")
                with self.assertRaisesRegex(ValueError, "exceeds 8192 bytes"):
                    catalog.load_demo_index(path, sections)

    def test_demo_indexes_and_entries_are_closed(self):
        entry = {"name": "atlas.jpg", "sha256": "a" * 64}
        cases = (
            ({"assets": [entry], "description": "Extra"}, "invalid demo index fields"),
            ({}, "invalid demo index fields"),
            ({"assets": {}}, "must be a list"),
            ({"assets": [{**entry, "extra": True}]}, "invalid demo asset fields"),
            ({"assets": [{"name": "atlas.jpg"}]}, "invalid demo asset fields"),
            ({"assets": [{**entry, "name": 1}]}, "must be strings"),
            ({"assets": [{**entry, "sha256": "A" * 64}]}, "invalid demo asset SHA-256"),
        )
        for changes, reason in cases:
            with self.subTest(changes=changes):
                path = self.write_json("index.json", {"schemaVersion": 1, **changes})
                with self.assertRaisesRegex(ValueError, reason):
                    catalog.load_demo_index(path, ("assets",))

    def test_playback_gallery_scenes_need_both_variants(self):
        previews = [
            {"path": f"previews/{scene}-{variant}.webp", "role": catalog.PREVIEW_ROLES.get(scene, "other")}
            for scene in catalog.BASE_SCENES for variant in ("normal", "glass")
        ]
        manifest = {"title": "Demo", "previews": previews}
        previews.append({"path": "previews/extra-photo-normal.webp", "role": "other"})
        catalog.validate_gallery(manifest)
        previews.append({"path": "previews/player-playing-normal.webp", "role": "other"})
        with self.assertRaisesRegex(ValueError, "player-playing-glass"):
            catalog.validate_gallery(manifest)
        previews.append({"path": "previews/player-playing-glass.webp", "role": "other"})
        catalog.validate_gallery(manifest)

    def test_empty_files_are_rejected_even_with_matching_hash(self):
        path = self.folder / "empty.webp"
        path.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            catalog.check_hash(path, hashlib.sha256(b"").hexdigest(), catalog.PREVIEW_LIMIT)

    def test_snapshot_checks_serialized_size_and_raw_total(self):
        index = self.write_json("index.json", {"schemaVersion": 1, "themes": []})
        catalog.validate_snapshot(index, [])
        with patch.object(catalog, "CATALOG_SNAPSHOT_LIMIT", 64):
            with self.assertRaisesRegex(ValueError, "snapshot"):
                catalog.validate_snapshot(index, [])
            first = self.folder / "first.json"
            second = self.folder / "second.json"
            first.write_bytes(b"x" * 32)
            second.write_bytes(b"x" * 32)
            with self.assertRaisesRegex(ValueError, "exceeds"):
                catalog.validate_snapshot(index, [first, second])

    def test_metadata_integer_bounds_match_android_types(self):
        fields = [
            ("theme", "properties.author.properties.githubId", 2**63 - 1),
            ("theme", "properties.revision", 2**31 - 1),
            ("theme", "properties.compatibility.properties.minVersionCode", 2**31 - 1),
            ("catalog", "properties.themes.items.properties.revision", 2**31 - 1),
            ("creator", "properties.githubId", 2**63 - 1),
            ("creators-index", "properties.creators.items.properties.githubId", 2**63 - 1),
            ("blocked-creators", "properties.accounts.items.properties.githubId", 2**63 - 1),
            ("verified-creators", "properties.githubIds.items", 2**63 - 1),
            ("reviews", "properties.reviews.items.properties.author.properties.githubId", 2**63 - 1),
        ]
        for name, path, maximum in fields:
            with self.subTest(schema=name, path=path):
                schema = catalog.load_schema(name).schema
                for component in path.split("."):
                    schema = schema[component]
                validator = Draft202012Validator(schema)
                self.assertTrue(validator.is_valid(maximum))
                self.assertFalse(validator.is_valid(maximum + 1))

    def test_supplementary_characters_count_once_in_metadata(self):
        symbol = "\U0001f3b5"
        self.manifest.update(title=symbol * 100, description=symbol * 1000, tags=[symbol * 30])
        validator = catalog.load_schema("theme")
        self.assertTrue(validator.is_valid(self.manifest))
        self.manifest["title"] += symbol
        self.assertFalse(validator.is_valid(self.manifest))


if __name__ == "__main__":
    unittest.main()
