"""Content-addressed asset storage and derived image renditions (design §5.5/§5.7).

The ``assets`` table holds metadata; original bytes live in a CAS directory
next to the database file at ``assets/<hash[:2]>/<hash>`` (disaster recovery
is ``DB + assets/ = everything``). This is the thinnest registration needed
to make renditions real — the full ingestion pipeline (text extraction,
chunking, source/claim proposals) is Phase 4.

Renditions (§5.7) are derived, regenerable WebP images keyed by
``sha256(asset_hash + ':' + profile)``, lazily generated on first request,
cached on disk at ``renditions/<id[:2]>/<id>.webp``, and evictable via
:func:`purge_renditions`. Two profiles exist (``thumb``, ``preview``);
``page:<n>`` PDF rasters are Phase 4. **LLMs never receive original
binaries** — the MCP server serves renditions and metadata only.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from nodum import db
from nodum.models import AssetOut, PurgeResult, RenditionOut

#: MIME type every rendition is encoded as (design §5.7).
RENDITION_MIME = "image/webp"


class AssetNotFound(LookupError):
    """Raised when an asset hash or asset-reference id does not resolve."""


class UnsupportedRendition(ValueError):
    """Raised when a rendition cannot be produced (unknown profile or non-image asset)."""


@dataclass(frozen=True)
class Profile:
    """One rendition profile: geometry cap, WebP quality, optional size target.

    ``target_bytes`` triggers a quality-stepping loop that re-encodes at
    progressively lower qualities until the output fits (or the floor is
    reached — the smallest encode wins).
    """

    max_edge: int
    quality: int
    target_bytes: int | None = None


#: The Phase-2 rendition profiles (design §5.7). ``page:<n>`` rasters are
#: Phase 4; ``full`` is never a rendition — originals are HTTP-API-only.
PROFILES = {
    "thumb": Profile(max_edge=256, quality=75),
    "preview": Profile(max_edge=1024, quality=80, target_bytes=300_000),
}

#: Quality ladder for fitting a ``target_bytes`` cap, starting from the
#: profile's nominal quality.
_QUALITY_STEPS = (80, 70, 60, 50, 40, 30, 20)


def data_dir(path: str | Path | None = None) -> Path:
    """Return the data directory (the database file's parent) for a DB path.

    Originals and renditions live under it, so a database move carries its
    store with it. Raises for ``:memory:`` databases — the CAS needs a
    filesystem location.
    """
    db_file = Path(path).expanduser() if path is not None else db.db_path()
    if str(db_file) == ":memory:":
        raise ValueError("asset storage requires a file-backed database")
    return db_file.parent


def _connect(path: str | Path | None) -> sqlite3.Connection:
    """Open a connection and apply any pending migrations (idempotent)."""
    conn = db.connect(path)
    db.init_db(conn)
    return conn


def _asset_out(row: sqlite3.Row) -> AssetOut:
    """Build the public asset model from an assets row."""
    return AssetOut(
        hash=row["hash"],
        mime=row["mime"],
        size_bytes=row["size_bytes"],
        original_name=row["original_name"],
        extracted_text=row["extracted_text"],
        created_at=row["created_at"],
    )


def _original_path(directory: Path, asset_hash: str) -> Path:
    """Return the CAS path of an original: ``assets/<hash[:2]>/<hash>``."""
    return directory / "assets" / asset_hash[:2] / asset_hash


def rendition_id(asset_hash: str, profile: str) -> str:
    """Return the deterministic rendition id: ``sha256(asset_hash + ':' + profile)``."""
    return hashlib.sha256(f"{asset_hash}:{profile}".encode()).hexdigest()


def _rendition_rel_path(rid: str) -> Path:
    """Return the DB-stored, data-dir-relative cache path of a rendition."""
    return Path("renditions") / rid[:2] / f"{rid}.webp"


def register_asset(
    source: str | Path,
    name: str | None = None,
    path: str | Path | None = None,
) -> AssetOut:
    """Register a local file as a content-addressed asset and return its metadata.

    Copies the bytes into the CAS directory keyed by their sha256. Content
    addressing makes registration idempotent: re-registering the same bytes
    returns the existing row without moving data (dedup).

    Args:
        source: Path to the local file to register.
        name: Original name to record (defaults to the source file's name);
            also the hint for MIME guessing (``application/octet-stream``
            when nothing matches).
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        The asset's metadata row (newly created or pre-existing).
    """
    source_file = Path(source).expanduser()
    data = source_file.read_bytes()
    asset_hash = hashlib.sha256(data).hexdigest()
    original_name = name or source_file.name
    directory = data_dir(path)

    conn = _connect(path)
    try:
        existing = conn.execute("SELECT * FROM assets WHERE hash = ?", (asset_hash,)).fetchone()
        if existing is not None:
            return _asset_out(existing)

        destination = _original_path(directory, asset_hash)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            # Temp-then-rename so a crash mid-copy never leaves a partial CAS entry.
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, destination)

        mime = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        conn.execute(
            """
            INSERT INTO assets (hash, mime, size_bytes, original_name)
            VALUES (?, ?, ?, ?)
            """,
            (asset_hash, mime, len(data), original_name),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM assets WHERE hash = ?", (asset_hash,)).fetchone()
        return _asset_out(row)
    finally:
        conn.close()


def _resolve_hash(conn: sqlite3.Connection, id_or_hash: str) -> str:
    """Resolve an asset hash directly or through an asset-reference node's props."""
    row = conn.execute("SELECT hash FROM assets WHERE hash = ?", (id_or_hash,)).fetchone()
    if row is not None:
        return row["hash"]
    node = conn.execute(
        "SELECT props FROM nodes WHERE id = ? AND state != 'archived'", (id_or_hash,)
    ).fetchone()
    if node is not None:
        asset_hash = json.loads(node["props"]).get("asset_hash")
        if (
            asset_hash
            and conn.execute("SELECT 1 FROM assets WHERE hash = ?", (asset_hash,)).fetchone()
        ):
            return asset_hash
    raise AssetNotFound(f"asset not found: {id_or_hash}")


def get_asset(id_or_hash: str, path: str | Path | None = None) -> AssetOut:
    """Fetch one asset's metadata by hash or by an asset-reference node's id.

    Raises:
        AssetNotFound: If neither an asset nor a node with an ``asset_hash``
            prop resolves.
    """
    conn = _connect(path)
    try:
        asset_hash = _resolve_hash(conn, id_or_hash)
        row = conn.execute("SELECT * FROM assets WHERE hash = ?", (asset_hash,)).fetchone()
        return _asset_out(row)
    finally:
        conn.close()


def list_assets(path: str | Path | None = None) -> list[AssetOut]:
    """List every registered asset in registration order."""
    conn = _connect(path)
    try:
        rows = conn.execute("SELECT * FROM assets ORDER BY created_at, hash").fetchall()
        return [_asset_out(row) for row in rows]
    finally:
        conn.close()


def _prepare_image(original: Path, profile: Profile) -> Image.Image:
    """Open an original and return the downscaled image (never upscaled)."""
    with Image.open(original) as image:
        transposed = ImageOps.exif_transpose(image)
        if transposed.mode not in ("RGB", "RGBA"):
            has_alpha = "A" in transposed.getbands() or "transparency" in transposed.info
            transposed = transposed.convert("RGBA" if has_alpha else "RGB")
        # thumbnail() only ever shrinks — images under the cap keep their size.
        transposed.thumbnail((profile.max_edge, profile.max_edge), Image.LANCZOS)
        return transposed


def _encode_webp(image: Image.Image, profile: Profile) -> bytes:
    """Encode as WebP, stepping quality down to fit the profile's size target."""
    qualities = [q for q in _QUALITY_STEPS if q <= profile.quality] or [profile.quality]
    smallest = b""
    for quality in qualities:
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=quality)
        encoded = buffer.getvalue()
        if not smallest or len(encoded) < len(smallest):
            smallest = encoded
        if profile.target_bytes is None or len(encoded) <= profile.target_bytes:
            return encoded
    return smallest


def get_rendition(
    id_or_hash: str,
    profile: str = "preview",
    include_data: bool = False,
    path: str | Path | None = None,
) -> RenditionOut:
    """Fetch an image rendition, generating and caching it on first request.

    Args:
        id_or_hash: Asset hash, or the id of a node with an ``asset_hash`` prop.
        profile: ``thumb`` or ``preview`` (see :data:`PROFILES`).
        include_data: Embed the WebP bytes as base64 in the result (the MCP
            path); otherwise only metadata and the cache ``path`` are returned.
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        The rendition metadata; ``cached`` reports whether this call hit the
        on-disk cache or (re)generated the image.

    Raises:
        AssetNotFound: If the asset does not resolve.
        UnsupportedRendition: If the profile is unknown or the asset is not a
            raster image Pillow can read.
    """
    spec = PROFILES.get(profile)
    if spec is None:
        raise UnsupportedRendition(
            f"unknown rendition profile: {profile!r} (have: {', '.join(sorted(PROFILES))})"
        )

    directory = data_dir(path)
    conn = _connect(path)
    try:
        asset_hash = _resolve_hash(conn, id_or_hash)
        asset = conn.execute("SELECT * FROM assets WHERE hash = ?", (asset_hash,)).fetchone()
        if not asset["mime"].startswith("image/"):
            raise UnsupportedRendition(
                f"renditions are only supported for image assets, got {asset['mime']}"
            )

        rid = rendition_id(asset_hash, profile)
        cached_row = conn.execute("SELECT * FROM renditions WHERE id = ?", (rid,)).fetchone()
        cache_file = directory / _rendition_rel_path(rid)
        if cached_row is not None and cache_file.exists():
            return _rendition_out(cached_row, cache_file, cached=True, include_data=include_data)

        original = _original_path(directory, asset_hash)
        if not original.exists():
            raise AssetNotFound(f"asset bytes missing from the CAS: {original}")
        try:
            image = _prepare_image(original, spec)
        except UnidentifiedImageError as exc:
            raise UnsupportedRendition(
                f"cannot render {asset['mime']} ({asset['original_name']}): not a raster image"
            ) from exc
        encoded = _encode_webp(image, spec)

        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, cache_file)
        relative = _rendition_rel_path(rid).as_posix()
        conn.execute(
            """
            INSERT INTO renditions (id, asset_hash, profile, path, width, height, size_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                path = excluded.path,
                width = excluded.width,
                height = excluded.height,
                size_bytes = excluded.size_bytes
            """,
            (rid, asset_hash, profile, relative, image.width, image.height, len(encoded)),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM renditions WHERE id = ?", (rid,)).fetchone()
        return _rendition_out(row, cache_file, cached=False, include_data=include_data)
    finally:
        conn.close()


def _rendition_out(
    row: sqlite3.Row, cache_file: Path, cached: bool, include_data: bool
) -> RenditionOut:
    """Build the public rendition model, optionally embedding the WebP bytes."""
    data_base64 = None
    if include_data:
        data_base64 = base64.b64encode(cache_file.read_bytes()).decode()
    return RenditionOut(
        id=row["id"],
        asset_hash=row["asset_hash"],
        profile=row["profile"],
        mime=RENDITION_MIME,
        width=row["width"],
        height=row["height"],
        size_bytes=row["size_bytes"],
        path=str(cache_file),
        cached=cached,
        data_base64=data_base64,
    )


def read_rendition_bytes(rendition: RenditionOut) -> bytes:
    """Return a rendition's WebP bytes, from ``data_base64`` or the cache file."""
    if rendition.data_base64 is not None:
        return base64.b64decode(rendition.data_base64)
    return Path(rendition.path).read_bytes()


def purge_renditions(
    asset_hash: str | None = None,
    path: str | Path | None = None,
) -> PurgeResult:
    """Evict cached renditions (rows and files); they regenerate on next request.

    Args:
        asset_hash: Limit the purge to one asset's renditions (all profiles);
            ``None`` purges every cached rendition.
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        How many renditions were evicted and how many bytes were freed.
    """
    directory = data_dir(path)
    conn = _connect(path)
    try:
        if asset_hash is not None:
            rows = conn.execute(
                "SELECT id, path, size_bytes FROM renditions WHERE asset_hash = ?",
                (asset_hash,),
            ).fetchall()
            conn.execute("DELETE FROM renditions WHERE asset_hash = ?", (asset_hash,))
        else:
            rows = conn.execute("SELECT id, path, size_bytes FROM renditions").fetchall()
            conn.execute("DELETE FROM renditions")
        conn.commit()

        freed = 0
        for row in rows:
            cache_file = directory / row["path"]
            if cache_file.exists():
                freed += cache_file.stat().st_size
                cache_file.unlink()
        # Drop the now-empty two-hex fan-out directories (not `renditions/` itself).
        renditions_root = directory / "renditions"
        if renditions_root.exists():
            for child in renditions_root.iterdir():
                if child.is_dir() and not any(child.iterdir()):
                    child.rmdir()
        return PurgeResult(purged=len(rows), bytes_freed=freed)
    finally:
        conn.close()


def copy_rendition(rendition: RenditionOut, destination: str | Path) -> Path:
    """Copy a rendition's cached WebP file to ``destination`` (the CLI's --out)."""
    target = Path(destination).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(rendition.path, target)
    return target
