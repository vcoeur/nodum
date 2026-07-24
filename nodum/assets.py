"""Content-addressed asset storage and derived image renditions (design §5.5/§5.7).

The ``assets`` table holds metadata and ``asset_blobs`` holds the bytes, both
in the one database file — disaster recovery is ``DB = everything``. Keeping
bytes in a separate table from metadata means metadata queries and FTS never
scan blob overflow pages. Content addressing by sha256 is what makes
registration idempotent, and it is unaffected by where the bytes live. This is
the thinnest registration needed to make renditions real — the full ingestion
pipeline (text extraction, chunking, source/claim proposals) is Phase 4.

Originals are streamed in and out through :meth:`sqlite3.Connection.blobopen`,
so neither registration nor rendering ever holds a whole file in memory. The
copy is re-hashed as it streams: content addressing is only worth something if
the stored bytes really do hash to their key, and the file is read twice.
A single asset cannot exceed ``SQLITE_LIMIT_LENGTH`` (1 GB) — checked up
front. Note that the streamed copy holds SQLite's single write lock for its
whole duration, so a very large registration blocks other writers.

Renditions (§5.7) are derived, regenerable WebP images keyed by
``sha256(asset_hash + ':' + profile)``, lazily generated on first request,
stored as bytes in the ``renditions`` table, and evictable via
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
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from nodum import db
from nodum.models import AssetOut, PurgeResult, RenditionOut

#: MIME type every rendition is encoded as (design §5.7).
RENDITION_MIME = "image/webp"

#: Chunk size for streaming originals into and out of the blob store.
_CHUNK_BYTES = 1 << 20


class AssetNotFound(LookupError):
    """Raised when an asset hash or asset-reference id does not resolve."""


class UnsupportedRendition(ValueError):
    """Raised when a rendition cannot be produced (unknown profile or non-image asset)."""


class AssetTooLarge(ValueError):
    """Raised when a file exceeds SQLite's maximum blob length."""


class AssetSourceChanged(ValueError):
    """Raised when the source file changed between the hash pass and the copy.

    Registration reads the file twice; a file still being written, rotated, or
    truncated in between would otherwise be stored under the sha256 of bytes
    it no longer matches.
    """


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

#: Fallback qualities tried *below* a profile's nominal quality when its
#: ``target_bytes`` cap is not met. The ladder an encode actually walks always
#: starts at the profile's own quality (see :func:`_encode_webp`), so a nominal
#: value absent from this tuple is still the first encode attempted.
_QUALITY_STEPS = (80, 70, 60, 50, 40, 30, 20)


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


def rendition_id(asset_hash: str, profile: str) -> str:
    """Return the deterministic rendition id: ``sha256(asset_hash + ':' + profile)``."""
    return hashlib.sha256(f"{asset_hash}:{profile}".encode()).hexdigest()


def _hash_file(source_file: Path) -> tuple[str, int]:
    """Return a file's sha256 and byte length, reading it in chunks."""
    digest = hashlib.sha256()
    size = 0
    with source_file.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def open_original(conn: sqlite3.Connection, asset_hash: str, readonly: bool = True) -> sqlite3.Blob:
    """Open a seekable handle on an original's bytes without loading them.

    ``blobopen`` addresses a blob by rowid, so the caller's hash is resolved
    through ``asset_blobs`` first.

    Raises:
        AssetNotFound: If no blob row exists for the hash.
    """
    row = conn.execute("SELECT rowid FROM asset_blobs WHERE hash = ?", (asset_hash,)).fetchone()
    if row is None:
        raise AssetNotFound(f"asset bytes missing from the blob store: {asset_hash}")
    return conn.blobopen("asset_blobs", "data", row["rowid"], readonly=readonly)


def max_blob_bytes(conn: sqlite3.Connection) -> int:
    """Return this connection's maximum blob length (``SQLITE_LIMIT_LENGTH``).

    Typically 1 GB — the ceiling on a single registered asset, since an
    original is one blob value.
    """
    return conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)


def register_asset(
    source: str | Path,
    *,
    name: str | None = None,
    path: str | Path | None = None,
) -> AssetOut:
    """Register a local file as a content-addressed asset and return its metadata.

    Streams the bytes into the blob store keyed by their sha256, reading the
    source twice (once to hash, once to copy) so a large file is never held in
    memory. Content addressing makes registration idempotent: re-registering
    the same bytes returns the existing row without moving data (dedup).

    The copy pass re-hashes what it writes and compares it with the key the
    first pass produced. The two passes see the same file only if nothing
    touched it in between — a file still being written, a rotating log, a
    partial download — and a mismatch is refused rather than stored, because
    the alternative is a row whose bytes do not hash to their own key.

    Args:
        source: Path to the local file to register.
        name: Original name to record (defaults to the source file's name);
            also the hint for MIME guessing (``application/octet-stream``
            when nothing matches).
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        The asset's metadata row (newly created or pre-existing).

    Raises:
        FileNotFoundError: If ``source`` does not exist.
        AssetTooLarge: If the file is larger than SQLite's blob limit.
        AssetSourceChanged: If the file changed between the two read passes.
    """
    source_file = Path(source).expanduser()
    asset_hash, size = _hash_file(source_file)
    original_name = name or source_file.name

    conn = _connect(path)
    try:
        limit = max_blob_bytes(conn)
        if size > limit:
            raise AssetTooLarge(
                f"{source_file} is {size} bytes; a single asset cannot exceed SQLite's "
                f"{limit}-byte blob limit — split the file or keep it outside the graph"
            )
        existing = conn.execute("SELECT * FROM assets WHERE hash = ?", (asset_hash,)).fetchone()
        if existing is not None:
            return _asset_out(existing)

        mime = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        # Metadata row, then a zero-filled blob of the right size, then the
        # bytes streamed into it — all in one transaction, so a crash mid-copy
        # rolls back rather than leaving a half-written asset.
        conn.execute(
            """
            INSERT INTO assets (hash, mime, size_bytes, original_name)
            VALUES (?, ?, ?, ?)
            """,
            (asset_hash, mime, size, original_name),
        )
        conn.execute(
            "INSERT INTO asset_blobs (hash, data) VALUES (?, zeroblob(?))",
            (asset_hash, size),
        )
        if size:
            _stream_into_blob(conn, source_file, asset_hash, size)
        conn.commit()
        row = conn.execute("SELECT * FROM assets WHERE hash = ?", (asset_hash,)).fetchone()
        return _asset_out(row)
    finally:
        conn.close()


def _stream_into_blob(
    conn: sqlite3.Connection, source_file: Path, asset_hash: str, size: int
) -> None:
    """Copy a file into its pre-sized blob, verifying it still hashes to its key.

    A source that *grew* since the hash pass is caught before its extra bytes
    overrun the zeroblob (which would otherwise surface as a raw ``ValueError:
    data longer than blob length``), and refused as :class:`AssetSourceChanged`
    like the others. A source that *shrank* leaves the blob's zero-filled tail
    in place, so the row would commit with ``sha256(stored) != assets.hash``;
    hashing the copied bytes catches that, and any in-place rewrite of the same
    length as well.

    Raises:
        AssetSourceChanged: If the source grew past its hashed size, fewer
            bytes arrived than the blob expects, or the copied bytes do not
            hash to ``asset_hash``.
    """
    digest = hashlib.sha256()
    copied = 0
    with (
        open_original(conn, asset_hash, readonly=False) as blob,
        source_file.open("rb") as handle,
    ):
        while chunk := handle.read(_CHUNK_BYTES):
            if copied + len(chunk) > size:
                raise AssetSourceChanged(
                    f"{source_file} changed while it was being registered "
                    f"({size} bytes hashed as {asset_hash}, then grew past {size} bytes on the "
                    "copy pass) — nothing was stored; register it again once it is stable"
                )
            blob.write(chunk)
            digest.update(chunk)
            copied += len(chunk)
    if copied != size or digest.hexdigest() != asset_hash:
        raise AssetSourceChanged(
            f"{source_file} changed while it was being registered "
            f"({size} bytes hashed as {asset_hash}, then {copied} bytes copied as "
            f"{digest.hexdigest()}) — nothing was stored; register it again once it is stable"
        )


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


def get_asset(id_or_hash: str, *, path: str | Path | None = None) -> AssetOut:
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


def list_assets(*, path: str | Path | None = None) -> list[AssetOut]:
    """List every registered asset in registration order."""
    conn = _connect(path)
    try:
        rows = conn.execute("SELECT * FROM assets ORDER BY created_at, hash").fetchall()
        return [_asset_out(row) for row in rows]
    finally:
        conn.close()


class _BlobReader(io.RawIOBase):
    """Seekable file-like view over a blob handle.

    ``sqlite3.Blob`` raises on a seek past the end, while Pillow's format
    probing relies on file semantics, where seeking beyond EOF is legal and
    the read that follows simply comes back short. Probing an unrecognised
    file would otherwise fail with the wrong error, and a valid image could
    fail outright if a mismatched plugin probed past the end before the right
    one matched.
    """

    def __init__(self, blob: sqlite3.Blob) -> None:
        self._blob = blob
        self._size = len(blob)
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview) -> int:
        if self._position >= self._size:
            return 0
        self._blob.seek(self._position)
        chunk = self._blob.read(min(len(buffer), self._size - self._position))
        buffer[: len(chunk)] = chunk
        self._position += len(chunk)
        return len(chunk)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        origin = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._size}[whence]
        self._position = max(0, origin + offset)
        return self._position

    def tell(self) -> int:
        return self._position


def _prepare_image(original: sqlite3.Blob, profile: Profile) -> Image.Image:
    """Read an original from its blob handle and return the downscaled image.

    Pillow reads through the blob handle, so only the decoded image — not the
    stored file — is ever held in memory.
    """
    with Image.open(io.BufferedReader(_BlobReader(original))) as image:
        transposed = ImageOps.exif_transpose(image)
        if transposed.mode not in ("RGB", "RGBA"):
            has_alpha = "A" in transposed.getbands() or "transparency" in transposed.info
            transposed = transposed.convert("RGBA" if has_alpha else "RGB")
        # thumbnail() only ever shrinks — images under the cap keep their size.
        transposed.thumbnail((profile.max_edge, profile.max_edge), Image.LANCZOS)
        return transposed


def _encode_webp(image: Image.Image, profile: Profile) -> bytes:
    """Encode as WebP at the profile's quality, stepping down to fit its size target.

    The first attempt is always the profile's nominal quality, so a profile
    without a ``target_bytes`` cap (``thumb``) is encoded at exactly that
    quality and nothing else runs. A profile with a cap (``preview``) walks
    :data:`_QUALITY_STEPS` below its nominal quality until the output fits;
    if no step fits, the smallest encode wins.
    """
    qualities = [profile.quality, *(q for q in _QUALITY_STEPS if q < profile.quality)]
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
    *,
    profile: str = "preview",
    include_data: bool = False,
    path: str | Path | None = None,
) -> RenditionOut:
    """Fetch an image rendition, generating and caching it on first request.

    Args:
        id_or_hash: Asset hash, or the id of a node with an ``asset_hash`` prop.
        profile: ``thumb`` or ``preview`` (see :data:`PROFILES`).
        include_data: Embed the WebP bytes as base64 in the result (the MCP
            path); otherwise only metadata is returned and the stored bytes
            are never read.
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        The rendition metadata; ``cached`` reports whether this call hit the
        stored rendition or (re)generated the image.

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

    conn = _connect(path)
    try:
        asset_hash = _resolve_hash(conn, id_or_hash)
        asset = conn.execute("SELECT * FROM assets WHERE hash = ?", (asset_hash,)).fetchone()
        if not asset["mime"].startswith("image/"):
            raise UnsupportedRendition(
                f"renditions are only supported for image assets, got {asset['mime']}"
            )

        rid = rendition_id(asset_hash, profile)
        # Metadata only — the blob column is read solely when include_data is set.
        cached_row = conn.execute(
            """
            SELECT id, asset_hash, profile, width, height, size_bytes
            FROM renditions WHERE id = ?
            """,
            (rid,),
        ).fetchone()
        if cached_row is not None:
            return _rendition_out(conn, cached_row, cached=True, include_data=include_data)

        try:
            with open_original(conn, asset_hash) as original:
                image = _prepare_image(original, spec)
        except UnidentifiedImageError as exc:
            raise UnsupportedRendition(
                f"cannot render {asset['mime']} ({asset['original_name']}): not a raster image"
            ) from exc
        encoded = _encode_webp(image, spec)

        conn.execute(
            """
            INSERT INTO renditions (id, asset_hash, profile, data, width, height, size_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                data = excluded.data,
                width = excluded.width,
                height = excluded.height,
                size_bytes = excluded.size_bytes
            """,
            (rid, asset_hash, profile, encoded, image.width, image.height, len(encoded)),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, asset_hash, profile, width, height, size_bytes
            FROM renditions WHERE id = ?
            """,
            (rid,),
        ).fetchone()
        return _rendition_out(conn, row, cached=False, include_data=include_data)
    finally:
        conn.close()


def _rendition_out(
    conn: sqlite3.Connection, row: sqlite3.Row, cached: bool, include_data: bool
) -> RenditionOut:
    """Build the public rendition model, optionally embedding the WebP bytes."""
    data_base64 = None
    if include_data:
        data_base64 = base64.b64encode(_rendition_bytes(conn, row["id"])).decode()
    return RenditionOut(
        id=row["id"],
        asset_hash=row["asset_hash"],
        profile=row["profile"],
        mime=RENDITION_MIME,
        width=row["width"],
        height=row["height"],
        size_bytes=row["size_bytes"],
        cached=cached,
        data_base64=data_base64,
    )


def _rendition_bytes(conn: sqlite3.Connection, rid: str) -> bytes:
    """Read one rendition's stored WebP bytes."""
    row = conn.execute("SELECT data FROM renditions WHERE id = ?", (rid,)).fetchone()
    if row is None:
        raise AssetNotFound(f"rendition not found: {rid}")
    return row["data"]


def read_rendition_bytes(rendition: RenditionOut, *, path: str | Path | None = None) -> bytes:
    """Return a rendition's WebP bytes, from ``data_base64`` or the database.

    Args:
        rendition: The rendition to read.
        path: Explicit database path, used only when the rendition was
            fetched without ``include_data``.
    """
    if rendition.data_base64 is not None:
        return base64.b64decode(rendition.data_base64)
    conn = _connect(path)
    try:
        return _rendition_bytes(conn, rendition.id)
    finally:
        conn.close()


def purge_renditions(
    *,
    asset_hash: str | None = None,
    path: str | Path | None = None,
) -> PurgeResult:
    """Evict stored renditions; they regenerate on next request.

    The freed bytes stay inside the database file as reusable free pages —
    ``VACUUM`` returns them to the filesystem.

    Args:
        asset_hash: Limit the purge to one asset's renditions (all profiles);
            ``None`` purges every stored rendition.
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        How many renditions were evicted and how many bytes they held.
    """
    conn = _connect(path)
    try:
        if asset_hash is not None:
            rows = conn.execute(
                "SELECT size_bytes FROM renditions WHERE asset_hash = ?",
                (asset_hash,),
            ).fetchall()
            conn.execute("DELETE FROM renditions WHERE asset_hash = ?", (asset_hash,))
        else:
            rows = conn.execute("SELECT size_bytes FROM renditions").fetchall()
            conn.execute("DELETE FROM renditions")
        conn.commit()
        return PurgeResult(purged=len(rows), bytes_freed=sum(row["size_bytes"] for row in rows))
    finally:
        conn.close()


def copy_rendition(
    rendition: RenditionOut, destination: str | Path, *, path: str | Path | None = None
) -> Path:
    """Write a rendition's stored WebP bytes to ``destination`` (the CLI's --out)."""
    target = Path(destination).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(read_rendition_bytes(rendition, path=path))
    return target
