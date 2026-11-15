import hashlib
import io
import json
import unicodedata
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError, validators
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog"
CATALOG_LIMIT = 512 * 1024
THEME_DOCUMENT_LIMIT = 128 * 1024
PREVIEW_LIMIT = 4 * 1024 * 1024
CATALOG_SNAPSHOT_LIMIT = 16 * 1024 * 1024


def validate_metadata_const(validator, expected, value, schema):
    if type(expected) is int and type(value) is not int:
        yield ValidationError(f"{value!r} must use JSON integer notation")
        return
    yield from Draft202012Validator.VALIDATORS["const"](validator, expected, value, schema)


MetadataValidator = validators.extend(
    Draft202012Validator,
    validators={"const": validate_metadata_const},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda _, value: type(value) is int),
)


def read_limited(path, limit):
    with path.open("rb") as source:
        content = source.read(limit + 1)
    if not 0 < len(content) <= limit:
        raise ValueError(f"{path}: file is empty or exceeds {limit} bytes")
    return content


def load_json(path, limit=CATALOG_LIMIT):
    def reject_nonfinite(value):
        raise ValueError(f"{path}: {value} is not a JSON number")

    def unique_members(pairs):
        members = {}
        for name, value in pairs:
            if name in members:
                raise ValueError(f"{path}: duplicate JSON member {name!r}")
            members[name] = value
        return members

    try:
        content = read_limited(path, limit).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{path}: expected UTF-8 JSON") from error
    if content.startswith("\ufeff"):
        raise ValueError(f"{path}: UTF-8 BOM is unsupported")
    return json.loads(content, parse_constant=reject_nonfinite, object_pairs_hook=unique_members)


def load_schema(name):
    schema = load_json(CATALOG / "schemas" / "v1" / f"{name}.schema.json")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator if name == "appearance" else MetadataValidator
    return validator(schema)


def validate_json(path, validator, limit=CATALOG_LIMIT):
    document = load_json(path, limit)
    errors = list(validator.iter_errors(document))
    if errors:
        error = errors[0]
        raise ValueError(f"{path} at {error.json_path}: {error.message}")
    return document


def check_hash(path, expected, limit=CATALOG_LIMIT):
    content = read_limited(path, limit)
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError(f"{path}: SHA-256 mismatch")
    return content


def validate_image(path, expected):
    content = check_hash(path, expected, PREVIEW_LIMIT)
    expected_format = {".jpg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".gif": "GIF"}[path.suffix]
    try:
        with Image.open(io.BytesIO(content), formats=[expected_format]) as image:
            image.verify()
        with Image.open(io.BytesIO(content), formats=[expected_format]) as image:
            image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError(f"{path}: invalid {expected_format} image") from error


def validate_theme(folder, manifest_validator, appearance_validator):
    manifest = validate_json(folder / "manifest.json", manifest_validator)
    appearance = manifest["appearance"]
    appearance_path = folder / appearance["path"]
    check_hash(appearance_path, appearance["sha256"], THEME_DOCUMENT_LIMIT)
    validate_json(appearance_path, appearance_validator, THEME_DOCUMENT_LIMIT)

    preview_paths = set()
    for preview in manifest["previews"]:
        path = preview["path"]
        if path in preview_paths:
            raise ValueError(f"{folder}: duplicate preview {path}")
        preview_paths.add(path)
        validate_image(folder / path, preview["sha256"])
    return manifest


BASE_SCENES = ("player", "queue", "lyrics", "home", "navigation", "album", "artist",
               "playlist", "local-playlist", "search", "search-albums", "search-artists")
PREVIEW_ROLES = {"player": "player", "queue": "queue", "navigation": "navigation"}


def validate_reserved_tags(manifest, verified_ids):
    if manifest["author"]["githubId"] in verified_ids:
        return
    for tag in manifest["tags"]:
        normalized = "".join(char for char in unicodedata.normalize("NFKD", tag).casefold() if char.isalnum())
        if normalized in {"stock", "builtin"}:
            raise ValueError(f'{manifest["title"]}: tag {tag!r} is reserved for verified N-Zik creators')


def validate_gallery(manifest):
    previews = {}
    for preview in manifest["previews"]:
        stem = Path(preview["path"]).stem
        scene, _, variant = stem.rpartition("-")
        if not scene or variant not in {"normal", "glass"}:
            raise ValueError(f'{manifest["title"]}: name previews <screen>-normal or <screen>-glass')
        if stem in previews:
            raise ValueError(f'{manifest["title"]}: duplicate preview variant {stem}')
        if preview["role"] != PREVIEW_ROLES.get(scene, "other"):
            raise ValueError(f'{manifest["title"]}: incorrect preview role for {stem}')
        previews[stem] = preview
    missing = [f"{scene}-{variant}" for scene in BASE_SCENES
               for variant in ("normal", "glass") if f"{scene}-{variant}" not in previews]
    if missing:
        raise ValueError(f'{manifest["title"]}: missing previews: {", ".join(missing)}')
    for scene in ("player-playing", "miniplayer-playing"):
        normal, glass = f"{scene}-normal", f"{scene}-glass"
        if (normal in previews) != (glass in previews):
            missing_variant = glass if normal in previews else normal
            raise ValueError(f'{manifest["title"]}: missing preview counterpart {missing_variant}')


def validate_reviews(folder, validator):
    path = folder / "reviews.json"
    reviews = validate_json(path, validator)["reviews"]
    author_ids = [review["author"]["githubId"] for review in reviews]
    if len(author_ids) != len(set(author_ids)):
        raise ValueError(f"{path}: duplicate review author")


def validate_creator(folder, validator, expected_id=None):
    path = folder / "profile.json"
    profile = validate_json(path, validator)
    if expected_id is not None and str(profile["githubId"]) != expected_id:
        raise ValueError(f"{path}: GitHub ID differs from the creator directory")
    for field in ("avatar", "banner"):
        media = profile[field]
        if media["source"] == "custom":
            asset = media["asset"]
            validate_image(folder / asset["path"], asset["sha256"])
    return profile


def validate_snapshot(index_path, manifest_paths):
    index = read_limited(index_path, CATALOG_LIMIT)
    remaining = CATALOG_SNAPSHOT_LIMIT - len(index)
    manifests = []
    for path in manifest_paths:
        content = read_limited(path, min(CATALOG_LIMIT, remaining))
        remaining -= len(content)
        manifests.append(content.decode("utf-8"))
    snapshot = {"index": index.decode("utf-8"), "manifests": manifests,
                "syncedAt": 9223372036854775807}
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > CATALOG_SNAPSHOT_LIMIT:
        raise ValueError("catalog: encoded Android snapshot exceeds 16 MiB")


def validate_demo_assets():
    demo = ROOT / "demo" / "v1"
    palettes = ("atlas", "signal", "tide", "violet", "ember")
    covers = {f"{name}.jpg" for name in palettes}
    banners = {f"banners/{name}.webp" for name in palettes}

    def validate_assets(entries, expected):
        file_names = [entry["name"] for entry in entries]
        if len(file_names) != len(expected) or set(file_names) != expected:
            raise ValueError("demo: index does not match the required assets")
        for entry in entries:
            path = demo / entry["name"]
            if not 0 < path.stat().st_size <= 512 * 1024:
                raise ValueError(f"{path}: demo asset exceeds 512 KiB or is empty")
            check_hash(path, entry["sha256"])

    base = load_json(demo / "index.json")
    visuals = load_json(demo / "visuals.json")
    if any(type(index["schemaVersion"]) is not int or index["schemaVersion"] != 1 for index in (base, visuals)):
        raise ValueError("demo: unsupported index version")
    validate_assets(base["assets"], (covers - {"ember.jpg"}) | {"silence.mp3"})
    validate_assets(visuals["covers"], covers)
    validate_assets(visuals["banners"], banners)


def main():
    validate_demo_assets()
    manifest_validator = load_schema("theme")
    appearance_validator = load_schema("appearance")
    catalog = validate_json(CATALOG / "themes" / "index.json", load_schema("catalog"))
    reviews_validator = load_schema("reviews")
    creator_validator = load_schema("creator")
    official = validate_json(CATALOG / "creators" / "verified.json", load_schema("verified-creators"))
    verified_ids = set(official["githubIds"])
    blocked = validate_json(CATALOG / "creators" / "blocked.json", load_schema("blocked-creators"))
    blocked_ids = [entry["githubId"] for entry in blocked["accounts"]]
    if len(blocked_ids) != len(set(blocked_ids)):
        raise ValueError("creators: duplicate blocked GitHub ID")
    validate_creator(ROOT / "demo" / "examples" / "creator-v1", creator_validator)
    creators = {}
    creator_root = CATALOG / "creators"
    index = validate_json(creator_root / "index.json", load_schema("creators-index"))
    for entry in index["creators"]:
        github_id = entry["githubId"]
        if github_id in creators:
            raise ValueError(f"creators: duplicate GitHub ID {github_id}")
        folder = creator_root / str(github_id)
        check_hash(folder / "profile.json", entry["profileSha256"])
        creators[github_id] = validate_creator(folder, creator_validator, folder.name)
    if {p.name for p in creator_root.iterdir() if p.is_dir()} != {str(i) for i in creators}:
        raise ValueError("creators: index and profile directories differ")
    example = ROOT / "demo" / "examples" / "theme-v1"
    validate_theme(example, manifest_validator, appearance_validator)
    validate_reviews(example, reviews_validator)

    theme_ids = set()
    manifest_paths = []
    for entry in catalog["themes"]:
        theme_id = entry["themeId"]
        revision = entry["revision"]
        if theme_id in theme_ids:
            raise ValueError(f"catalog: duplicate theme ID {theme_id}")
        theme_ids.add(theme_id)
        folder = CATALOG / "themes" / theme_id / str(revision)
        manifest_paths.append(folder / "manifest.json")
        check_hash(folder / "manifest.json", entry["manifestSha256"])
        manifest = validate_theme(folder, manifest_validator, appearance_validator)
        validate_gallery(manifest)
        validate_reserved_tags(manifest, verified_ids)
        author = manifest["author"]
        creator = creators.get(author["githubId"])
        if creator and creator["login"].casefold() != author["login"].casefold():
            raise ValueError(f"{folder}: manifest author and creator GitHub logins differ")
        if (manifest["themeId"], manifest["revision"]) != (theme_id, revision):
            raise ValueError(f"{folder}: manifest ID or revision differs from the catalog")

    validate_snapshot(CATALOG / "themes" / "index.json", manifest_paths)

    for reviews in (CATALOG / "themes").glob("*/*/reviews.json"):
        if not (reviews.parent / "manifest.json").is_file():
            raise ValueError(f"{reviews}: missing revision manifest")
        validate_reviews(reviews.parent, reviews_validator)

    print(f"Validated the v1 schemas, examples, {len(theme_ids)} published themes and {len(creators)} creators.")


if __name__ == "__main__":
    main()
