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
    """The single-file promise: no CAS directory, no rendition cache on disk."""
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


def test_preview_respects_the_300kb_target(fresh_db, tmp_path):
    # Noise compresses badly: q80 WebP of this is far above 300 KB, forcing
    # the quality-stepping loop to fit the target.
    asset = _register_image(fresh_db, tmp_path, size=(1600, 1600), noise=True)
    rendition = assets.get_rendition(asset.hash, profile="preview")
    assert rendition.size_bytes <= 300_000
    assert (rendition.width, rendition.height) == (1024, 1024)


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
