"""Asset registration and rendition tests (design §5.5/§5.7).

Originals and renditions both live in the one database file. Renditions are
derived WebP images keyed by ``sha256(asset_hash + ':' + profile)``: lazily
generated, stored, evictable, and never upscaled. Tests generate their images
with Pillow — no network, no fixtures on disk.
"""

from __future__ import annotations

import hashlib
import io
import random
import sqlite3

import pytest
from PIL import Image

from nodum import assets, db, service
from nodum.assets import AssetNotFound, UnsupportedRendition


def _decode(rendition, path=None):
    """Open a rendition's stored WebP bytes as a Pillow image."""
    return Image.open(io.BytesIO(assets.read_rendition_bytes(rendition, path=path)))


def _make_image(path, size=(2000, 1000), mode="RGB", noise=False):
    """Write a deterministic test image (noise compresses badly on purpose)."""
    image = Image.new(mode, size)
    if noise:
        rng = random.Random(0)
        pixels = [
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(size[0] * size[1])
        ]
        image.putdata(pixels)
    image.save(path)
    return path


def _register_image(fresh_db, tmp_path, name="photo.png", **kwargs):
    """Register a generated image and return its AssetOut."""
    source = _make_image(tmp_path / name, **kwargs)
    return assets.register_asset(source)


def _webp_at(image, quality):
    """Encode a prepared image as WebP at one quality, exactly as the encoder does."""
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=quality)
    return buffer.getvalue()


def _prepared(db_path, asset_hash, profile):
    """Return the downscaled image the encoder is handed for a profile."""
    conn = db.connect(db_path)
    try:
        with assets.open_original(conn, asset_hash) as original:
            return assets._prepare_image(original, assets.PROFILES[profile])
    finally:
        conn.close()


# ── Registration (a metadata row + a blob + a sha256) ─────────────────────────


def test_register_streams_bytes_into_the_database(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    original = (tmp_path / "photo.png").read_bytes()
    assert asset.hash == hashlib.sha256(original).hexdigest()
    assert asset.mime == "image/png"
    assert asset.original_name == "photo.png"
    assert asset.size_bytes == len(original)

    conn = db.connect(fresh_db)
    try:
        stored = conn.execute(
            "SELECT data FROM asset_blobs WHERE hash = ?", (asset.hash,)
        ).fetchone()["data"]
    finally:
        conn.close()
    assert stored == original


def test_register_writes_nothing_beside_the_database(fresh_db, tmp_path):
    """The single-file promise: no asset directory, no rendition cache on disk."""
    asset = _register_image(fresh_db, tmp_path)
    assets.get_rendition(asset.hash, profile="thumb")
    beside = {child.name for child in fresh_db.parent.iterdir()}
    assert not {name for name in beside if name in ("assets", "renditions")}


def test_zero_byte_asset_registers(fresh_db, tmp_path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    asset = assets.register_asset(empty)
    assert asset.size_bytes == 0
    assert assets.get_asset(asset.hash).hash == asset.hash


def test_register_dedups_identical_content(fresh_db, tmp_path):
    first = _register_image(fresh_db, tmp_path)
    second = assets.register_asset(tmp_path / "photo.png", name="renamed.png")
    assert second.hash == first.hash
    assert second.original_name == "photo.png"  # the existing row wins
    assert len(assets.list_assets()) == 1


def test_register_distinct_content_gets_distinct_hashes(fresh_db, tmp_path):
    one = _register_image(fresh_db, tmp_path, name="a.png")
    other = _register_image(fresh_db, tmp_path, name="b.png", size=(10, 10))
    assert one.hash != other.hash
    assert len(assets.list_assets()) == 2


# ── Metadata resolution: by hash or by asset-reference node ──────────────────


def test_get_asset_by_hash_and_by_node_id(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    assert assets.get_asset(asset.hash).hash == asset.hash

    node = service.create_node(type="asset_ref", title="Photo", props={"asset_hash": asset.hash})
    assert assets.get_asset(node.id).hash == asset.hash


def test_get_asset_unknown_raises(fresh_db):
    with pytest.raises(AssetNotFound):
        assets.get_asset("missing")


# ── Geometry: downscale to the profile cap, never upscale ────────────────────


def test_thumb_downscales_to_256_max_edge(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, size=(2000, 1000))
    rendition = assets.get_rendition(asset.hash, profile="thumb")
    assert (rendition.width, rendition.height) == (256, 128)
    with _decode(rendition) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.size == (256, 128)


def test_preview_downscales_to_1024_max_edge(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, size=(2000, 1000))
    rendition = assets.get_rendition(asset.hash, profile="preview")
    assert (rendition.width, rendition.height) == (1024, 512)


def test_small_images_are_never_upscaled(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, size=(100, 50))
    for profile, expected in (("thumb", (100, 50)), ("preview", (100, 50))):
        rendition = assets.get_rendition(asset.hash, profile=profile)
        assert (rendition.width, rendition.height) == expected


def test_rgba_alpha_survives_rendition(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, mode="RGBA", size=(800, 800))
    rendition = assets.get_rendition(asset.hash, profile="preview")
    with _decode(rendition) as decoded:
        assert decoded.mode == "RGBA"


def test_thumb_is_encoded_at_the_profiles_nominal_quality(fresh_db, tmp_path):
    """`thumb` has no size target, so its q75 is the encode — not the ladder's q70.

    A WebP file records no quality factor, so the only way to pin this is to
    re-encode the same prepared image here and compare bytes.
    """
    asset = _register_image(fresh_db, tmp_path, size=(600, 300), noise=True)
    stored = assets.read_rendition_bytes(assets.get_rendition(asset.hash, profile="thumb"))

    prepared = _prepared(fresh_db, asset.hash, "thumb")
    assert assets.PROFILES["thumb"].quality == 75
    assert stored == _webp_at(prepared, 75)
    assert stored != _webp_at(prepared, 70)


def test_preview_encodes_at_q80_when_it_already_fits_the_target(fresh_db, tmp_path):
    """A target profile also starts at its nominal quality; steps are the fallback."""
    asset = _register_image(fresh_db, tmp_path, size=(400, 400))
    stored = assets.read_rendition_bytes(assets.get_rendition(asset.hash, profile="preview"))

    prepared = _prepared(fresh_db, asset.hash, "preview")
    assert len(stored) <= assets.PROFILES["preview"].target_bytes
    assert stored == _webp_at(prepared, 80)


def test_preview_respects_the_300kb_target(fresh_db, tmp_path):
    # Noise compresses badly: q80 WebP of this is far above 300 KB, forcing
    # the quality-stepping loop to fit the target.
    asset = _register_image(fresh_db, tmp_path, size=(1600, 1600), noise=True)
    rendition = assets.get_rendition(asset.hash, profile="preview")
    assert rendition.size_bytes <= 300_000
    assert (rendition.width, rendition.height) == (1024, 1024)
    # The nominal quality really was too big — the ladder is what fit it.
    assert len(_webp_at(_prepared(fresh_db, asset.hash, "preview"), 80)) > 300_000


# ── Modes WebP cannot encode directly are converted first ────────────────────


@pytest.mark.parametrize(
    ("mode", "name"),
    [("P", "palette.png"), ("L", "grayscale.png"), ("CMYK", "print.jpg")],
)
def test_non_rgb_originals_are_converted_before_encoding(fresh_db, tmp_path, mode, name):
    """Palette, grayscale and CMYK originals still render.

    Real files arrive in these modes — exported palettes, scans, print-ready
    JPEGs — and WebP takes none of them: without the conversion branch the
    rendition would fail on the first non-RGB upload.
    """
    source = tmp_path / name
    Image.new(mode, (300, 150)).save(source)
    with Image.open(source) as reopened:
        assert reopened.mode == mode  # the mode really survived the round-trip

    asset = assets.register_asset(source)
    rendition = assets.get_rendition(asset.hash, profile="thumb")
    assert (rendition.width, rendition.height) == (256, 128)
    with _decode(rendition) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.mode == "RGB"


def test_palette_transparency_becomes_alpha(fresh_db, tmp_path):
    """A palette image with a transparent index converts to RGBA, not RGB.

    Alpha lives in `info["transparency"]` for `P` mode, not in the bands, so
    only checking the bands would silently flatten transparent PNGs.
    """
    source = tmp_path / "transparent.png"
    image = Image.new("P", (200, 100), 1)
    image.putpalette([255, 0, 0] * 256)
    image.paste(0, (0, 0, 100, 100))  # half the image on the transparent index
    image.save(source, transparency=0)

    asset = assets.register_asset(source)
    with _decode(assets.get_rendition(asset.hash, profile="thumb")) as decoded:
        assert decoded.mode == "RGBA"
        assert decoded.convert("RGBA").getpixel((10, 10))[3] == 0  # still transparent


# ── Addressing, caching, eviction ─────────────────────────────────────────────


def test_rendition_id_is_sha256_of_hash_and_profile(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    rendition = assets.get_rendition(asset.hash, profile="thumb")
    expected = hashlib.sha256(f"{asset.hash}:thumb".encode()).hexdigest()
    assert rendition.id == expected
    assert rendition.mime == "image/webp"


def test_lazy_generation_then_cache_hit(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    generated = assets.get_rendition(asset.hash, profile="preview")
    assert generated.cached is False
    hit = assets.get_rendition(asset.hash, profile="preview")
    assert hit.cached is True
    assert hit.id == generated.id


def test_include_data_embeds_base64_webp(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, size=(10, 10))
    rendition = assets.get_rendition(asset.hash, profile="thumb", include_data=True)
    raw = assets.read_rendition_bytes(rendition)
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


def test_metadata_only_fetch_reads_bytes_from_the_database(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path, size=(10, 10))
    rendition = assets.get_rendition(asset.hash, profile="thumb")
    assert rendition.data_base64 is None
    raw = assets.read_rendition_bytes(rendition)
    assert raw[:4] == b"RIFF" and len(raw) == rendition.size_bytes


def test_purge_evicts_stored_renditions(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    assets.get_rendition(asset.hash, profile="thumb")
    assets.get_rendition(asset.hash, profile="preview")

    result = assets.purge_renditions()
    assert result.purged == 2
    assert result.bytes_freed > 0

    conn = db.connect(fresh_db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM renditions").fetchone()["n"] == 0
    finally:
        conn.close()
    # Derived data regenerates on the next request.
    assert assets.get_rendition(asset.hash, profile="thumb").cached is False


def test_purge_scoped_to_one_asset(fresh_db, tmp_path):
    one = _register_image(fresh_db, tmp_path, name="a.png")
    other = _register_image(fresh_db, tmp_path, name="b.png", size=(10, 10))
    assets.get_rendition(one.hash, profile="thumb")
    assets.get_rendition(other.hash, profile="thumb")

    result = assets.purge_renditions(asset_hash=one.hash)
    assert result.purged == 1
    assert assets.get_rendition(other.hash, profile="thumb").cached is True


# ── Calling convention ────────────────────────────────────────────────────────


def test_options_including_the_db_path_are_keyword_only(fresh_db, tmp_path):
    """Every public function follows the service/search convention.

    `path` used to be positional here alone, so `get_asset(x, y)` quietly read
    `y` as a database path where `service.get_node(x, y)` is a TypeError.
    """
    asset = _register_image(fresh_db, tmp_path)
    rendition = assets.get_rendition(asset.hash, profile="thumb")
    for call in (
        lambda: assets.register_asset(tmp_path / "photo.png", "renamed.png"),
        lambda: assets.get_asset(asset.hash, "not-a-database"),
        lambda: assets.list_assets(fresh_db),
        lambda: assets.get_rendition(asset.hash, "thumb"),
        lambda: assets.read_rendition_bytes(rendition, fresh_db),
        lambda: assets.purge_renditions(asset.hash),
        lambda: assets.copy_rendition(rendition, tmp_path / "out.webp", fresh_db),
    ):
        with pytest.raises(TypeError, match="positional"):
            call()


# ── Clean rejection ───────────────────────────────────────────────────────────


def test_non_image_assets_are_rejected(fresh_db, tmp_path):
    text_file = tmp_path / "notes.txt"
    text_file.write_text("plain text, not an image")
    asset = assets.register_asset(text_file)
    with pytest.raises(UnsupportedRendition, match="only supported for image assets"):
        assets.get_rendition(asset.hash)


def test_unreadable_image_bytes_are_rejected(fresh_db, tmp_path):
    fake = tmp_path / "broken.png"
    fake.write_bytes(b"definitely not a png")
    asset = assets.register_asset(fake)
    with pytest.raises(UnsupportedRendition, match="not a raster image"):
        assets.get_rendition(asset.hash)


def test_unknown_profile_is_rejected(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    with pytest.raises(UnsupportedRendition, match="unknown rendition profile"):
        assets.get_rendition(asset.hash, profile="page:1")


def test_rendition_of_missing_asset_raises(fresh_db):
    with pytest.raises(AssetNotFound):
        assets.get_rendition("missing")


# ── Streaming and the single-file promise ─────────────────────────────────────


def test_open_original_streams_the_stored_bytes(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    original = (tmp_path / "photo.png").read_bytes()
    conn = db.connect(fresh_db)
    try:
        with assets.open_original(conn, asset.hash) as blob:
            assert len(blob) == len(original)
            blob.seek(0)
            head = blob.read(16)
            blob.seek(0)
            assert blob.read() == original
        assert head == original[:16]
    finally:
        conn.close()


def test_open_original_of_missing_asset_raises(fresh_db):
    conn = db.connect(fresh_db)
    try:
        with pytest.raises(AssetNotFound):
            assets.open_original(conn, "missing")
    finally:
        conn.close()


def test_vacuum_into_snapshot_carries_originals_and_renditions(fresh_db, tmp_path, monkeypatch):
    """DB = everything: a one-file backup restores the binaries with the graph."""
    asset = _register_image(fresh_db, tmp_path)
    rendition = assets.get_rendition(asset.hash, profile="thumb")
    original = (tmp_path / "photo.png").read_bytes()

    snapshot = tmp_path / "backup" / "graph.db"
    snapshot.parent.mkdir()
    conn = db.connect(fresh_db)
    try:
        conn.execute("VACUUM INTO ?", (str(snapshot),))
    finally:
        conn.close()

    # Nothing but the one file is carried over.
    monkeypatch.setenv("NODUM_DB", str(snapshot))
    assert assets.get_asset(asset.hash).hash == asset.hash
    restored = db.connect(snapshot)
    try:
        with assets.open_original(restored, asset.hash) as blob:
            assert blob.read() == original
    finally:
        restored.close()
    assert assets.read_rendition_bytes(rendition)[:4] == b"RIFF"


def test_register_refuses_a_source_that_shrank_between_passes(fresh_db, tmp_path, monkeypatch):
    """Registration reads the file twice; the stored bytes must match the key.

    A file still being written (a rotating log, a partial download) can be
    shorter on the copy pass than on the hash pass. The blob keeps its
    zero-filled tail, so the row would commit with
    `sha256(stored) != assets.hash` — silently, and forever.
    """
    source = tmp_path / "rotating.log"
    source.write_bytes(b"the whole payload, every byte of it")
    hash_file = assets._hash_file

    def hash_then_truncate(path):
        digest_and_size = hash_file(path)
        path.write_bytes(b"truncated")  # the writer rotates the file underneath us
        return digest_and_size

    monkeypatch.setattr(assets, "_hash_file", hash_then_truncate)
    with pytest.raises(assets.AssetSourceChanged, match="changed while it was being registered"):
        assets.register_asset(source)

    monkeypatch.undo()
    assert assets.list_assets() == []
    conn = db.connect(fresh_db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM asset_blobs").fetchone()["n"] == 0
    finally:
        conn.close()


def test_register_refuses_a_source_that_grew_between_passes(fresh_db, tmp_path, monkeypatch):
    """A source that GROWS between the two passes is refused like a shrink/rewrite.

    The extra bytes would overrun the pre-sized blob and surface as a raw
    `ValueError: data longer than blob length`; registration must raise the
    tidy `AssetSourceChanged` the other cases raise, with nothing committed.
    """
    source = tmp_path / "growing.log"
    source.write_bytes(b"small")
    hash_file = assets._hash_file

    def hash_then_grow(path):
        digest_and_size = hash_file(path)
        path.write_bytes(b"small plus a great deal of freshly appended data")  # writer keeps going
        return digest_and_size

    monkeypatch.setattr(assets, "_hash_file", hash_then_grow)
    with pytest.raises(assets.AssetSourceChanged, match="changed while it was being registered"):
        assets.register_asset(source)

    monkeypatch.undo()
    assert assets.list_assets() == []
    conn = db.connect(fresh_db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM asset_blobs").fetchone()["n"] == 0
    finally:
        conn.close()


def test_stored_bytes_always_hash_to_their_key(fresh_db, tmp_path):
    asset = _register_image(fresh_db, tmp_path)
    conn = db.connect(fresh_db)
    try:
        with assets.open_original(conn, asset.hash) as blob:
            stored = blob.read()
    finally:
        conn.close()
    assert hashlib.sha256(stored).hexdigest() == asset.hash
    assert len(stored) == asset.size_bytes


def test_oversized_asset_is_refused_with_a_clear_message(fresh_db, tmp_path, monkeypatch):
    """The blob-length ceiling is named up front, not as `blob too big`."""
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 64)
    monkeypatch.setattr(assets, "max_blob_bytes", lambda conn: 16)

    with pytest.raises(assets.AssetTooLarge, match="cannot exceed SQLite's 16-byte blob limit"):
        assets.register_asset(source)
    assert assets.list_assets() == []


def test_register_missing_file_raises_file_not_found(fresh_db, tmp_path):
    with pytest.raises(FileNotFoundError):
        assets.register_asset(tmp_path / "nope.png")


def test_registration_rolls_back_when_the_blob_write_fails(fresh_db, tmp_path, monkeypatch):
    """A crash mid-copy must leave no asset row behind."""
    source = _make_image(tmp_path / "photo.png")

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("disk gone")

    monkeypatch.setattr(assets, "open_original", explode)
    with pytest.raises(sqlite3.OperationalError):
        assets.register_asset(source)

    monkeypatch.undo()
    assert assets.list_assets() == []
    conn = db.connect(fresh_db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM asset_blobs").fetchone()["n"] == 0
    finally:
        conn.close()
