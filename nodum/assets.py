"""Content-addressed asset storage and derived image renditions (design §5.5/§5.7).

The ``assets`` table holds metadata and ``asset_blobs`` holds the bytes, both
in the one database file — disaster recovery is ``DB = everything``. Keeping
bytes in a separate table from metadata means metadata queries and FTS never
scan blob overflow pages. Content addressing by sha256 is what makes
registration idempotent, and it is unaffected by where the bytes live. This
module owns *storage* only: registering bytes, deriving renditions, and
answering who may read them. Turning bytes into graph structure — extraction,
the describing node, the source node and its provenance edge — is
:mod:`nodum.ingest`, which composes this module with :mod:`nodum.extract` and
the service layer rather than writing anything itself.

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
:func:`purge_renditions`. Three profile shapes resolve (:func:`resolve_profile`):
the static ``thumb`` and ``preview``, which require an image asset, and
``page:<n>`` — a 1-based page of a PDF, rasterised at :data:`PAGE_DPI` and
then encoded down the *same* WebP path, so a page and a photograph share
their quality-stepping and size behaviour. A raster is an ordinary rendition
row: same id scheme, same lazy generation, same cache, same eviction.
**LLMs never receive original binaries** — the MCP server serves renditions
and metadata only.

``pypdfium2`` is the rasteriser because it is the maintained Python binding to
a production PDF engine (PDFium, the one in Chrome) that ships permissively
licensed wheels — Apache-2.0/BSD, no system package to install anywhere.
PyMuPDF renders at least as well and was rejected on its licence alone: AGPL
would reach anything that embeds nodum. The import happens lazily inside
:func:`_render_pdf_page` and the dependency sits behind the ``pdf`` extra, so
an install without it still serves image renditions and answers a page request
with an :class:`UnsupportedRendition` naming the extra rather than an
``ImportError`` at startup.

Rendering is bounded by pixel count, not just by file size: a decompression
bomb is a small file whose *decode* is enormous, so :data:`MAX_IMAGE_PIXELS`
is checked from the image header — before any decoding — both when a caller
offers bytes (:func:`check_image_pixel_budget`) and when a stored original is
about to be rendered (:func:`_prepare_image`). A page raster has no header to
read, so its budget is arithmetic instead: the bitmap PDFium allocates is the
page geometry times the DPI scale, and PDF permits a 200×200 inch page, which
is 829 megapixels at 144 DPI. :func:`sniff_image_mime` is the matching "what
is this really" helper: registration records the MIME ``mimetypes`` derives
from the *name*, which is fine for a local file the operator chose and useless
against a network upload.

**Access: an asset is as reachable as its describing nodes.** Asset rows are
keyed by sha256 and deduped globally, so a ``space_id`` column here could only
lie about the second space to register the same bytes. The per-space thing is
the ``asset_ref`` *node* — and migration 0009's unique index is already
``(asset_hash, space_id)`` over those nodes, one live describing node per hash
per space. Visibility follows from that: **a principal may read an asset iff it
can read at least one active ``asset_ref`` node carrying the hash**
(:func:`_readable_hashes`), which makes asset access an ordinary scoped graph
read rather than a rule of its own (Phase 4 note 01, D1 — it retires the Q13
interim "any read grant reaches every asset").

Bytes with no describing node are therefore invisible to every agent and
visible to humans, which is the right default for freshly registered bytes
whose ingestion has not run yet.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from nodum import db
from nodum.models import AssetOut, PurgeResult, RenditionOut
from nodum.principal import Principal
from nodum.store import Store

#: MIME type every rendition is encoded as (design §5.7).
RENDITION_MIME = "image/webp"

#: Chunk size for streaming originals into and out of the blob store.
_CHUNK_BYTES = 1 << 20

#: Largest image, in pixels, that a rendition is allowed to decode.
#:
#: 40 MP is roughly a 6300×6300 photograph — comfortably above anything a
#: camera or a screenshot produces and far below what makes a decode expensive
#: (each megapixel is ~4 MB resident as RGBA). Pillow's own
#: ``DecompressionBombWarning`` / ``DecompressionBombError`` pair is not a
#: usable guard on its own: it *warns* between 1× and 2× its threshold and
#: decodes anyway, which is exactly the 121 MP case that cost 185 MB of RSS.
MAX_IMAGE_PIXELS = 40_000_000

#: Image formats this system can render, and the magic bytes that identify
#: each. Sniffed rather than trusted: ``mimetypes.guess_type`` reads the
#: filename, which whoever supplied the bytes also chose.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)

#: Bytes read to identify a format. WebP needs 12 (``RIFF….WEBP``); the rest
#: are shorter.
_SNIFF_BYTES = 16


class AssetNotFound(LookupError):
    """Raised when an asset hash or asset-reference id does not resolve."""


class UnsupportedRendition(ValueError):
    """Raised when a rendition cannot be produced.

    Covers every "this request has no answer" case: an unknown profile name, a
    profile that does not match the asset's kind, a page past the end of a PDF,
    unreadable bytes, and the absent ``pypdfium2`` backend — all of which are
    the caller's request being unserviceable, not the server failing.
    """


class AssetTooLarge(ValueError):
    """Raised when a file exceeds SQLite's maximum blob length."""


class AssetSourceChanged(ValueError):
    """Raised when the source file changed between the hash pass and the copy.

    Registration reads the file twice; a file still being written, rotated, or
    truncated in between would otherwise be stored under the sha256 of bytes
    it no longer matches.
    """


class ImageTooLarge(ValueError):
    """Raised when an image's pixel count exceeds :data:`MAX_IMAGE_PIXELS`.

    A decompression bomb is small on disk and enormous in memory: a 612 KB PNG
    decoding to 14000×14000 is 196 megapixels, and a 375 KB one at 121 MP sits
    *below* Pillow's own bomb threshold and simply decodes, costing ~185 MB of
    resident memory on the event loop. Both are refused by pixel count, read
    from the image header before any decoding happens.
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


#: The static rendition profiles (design §5.7), the ones that name a whole
#: asset. ``page:<n>`` is the parameterised third (:data:`PAGE_PROFILE`) and
#: cannot live here because its page number is part of the name; ``full`` is
#: never a rendition — originals are HTTP-API-only.
PROFILES = {
    "thumb": Profile(max_edge=256, quality=75),
    "preview": Profile(max_edge=1024, quality=80, target_bytes=300_000),
}

#: The ``page:<n>`` rendition name, capturing a **1-based** page number.
#: ``page:0`` is not a page and deliberately fails to match, as does
#: ``page:01`` — one spelling per page, or two names would key two rendition
#: rows onto the same bitmap.
PAGE_PROFILE_RE = re.compile(r"^page:([1-9]\d*)$")

#: Rasterisation resolution for ``page:<n>`` (design §5.7).
#:
#: A PDF canvas unit is 1/72 inch, so 144 DPI is exactly 2× the page's own
#: coordinate space: 9 pt body text lands ~18 px tall, which is about the floor
#: at which a vision model reads a page rather than merely recognising its
#: layout. 216 DPI would buy legibility very little and cost 2.25× the pixels,
#: and every one of those pixels is paid for again as tokens.
PAGE_DPI = 144

#: Geometry and encoding for every ``page:<n>`` raster.
#:
#: ``max_edge`` 1568 — at :data:`PAGE_DPI` a US-Letter page renders 1224×1584
#: and A4 1191×1684, so the cap sits right at the long edge of an ordinary
#: page and barely bites; above it, current vision models downscale the image
#: themselves, so the extra pixels are transferred and then thrown away.
#: An outsized page is shrunk to fit rather than refused (only the pixel budget
#: refuses).
#: ``quality`` 85, one notch above ``preview``'s 80 — a page is hard-edged
#: glyphs on white, which is the first thing WebP's chroma handling smears.
#: ``target_bytes`` 500 KB ≈ ``preview``'s 300 KB scaled by the pixel ratio
#: (a letter page at the cap is ~1.9 MP against ``preview``'s ~1.05 MP), so
#: bytes-per-pixel stays comparable across profiles; a photographic scan walks
#: the same quality ladder down that an oversized ``preview`` does.
PAGE_PROFILE = Profile(max_edge=1568, quality=85, target_bytes=500_000)

#: Fallback qualities tried *below* a profile's nominal quality when its
#: ``target_bytes`` cap is not met. The ladder an encode actually walks always
#: starts at the profile's own quality (see :func:`_encode_webp`), so a nominal
#: value absent from this tuple is still the first encode attempted.
_QUALITY_STEPS = (80, 70, 60, 50, 40, 30, 20)


def resolve_profile(name: str) -> tuple[Profile, int | None]:
    """Resolve a rendition profile name to its spec and, for a raster, its page.

    Args:
        name: ``thumb``, ``preview``, or ``page:<n>`` with a 1-based ``n``.

    Returns:
        ``(spec, None)`` for a static profile and ``(spec, n)`` for a page
        raster. The page number is returned separately because it is the one
        thing about the request a :class:`Profile` cannot carry — every page
        of every PDF shares :data:`PAGE_PROFILE`.

    Raises:
        UnsupportedRendition: If the name is neither a static profile nor a
            well-formed ``page:<n>``.
    """
    static = PROFILES.get(name)
    if static is not None:
        return (static, None)
    match = PAGE_PROFILE_RE.match(name)
    if match is not None:
        return (PAGE_PROFILE, int(match.group(1)))
    raise UnsupportedRendition(
        f"unknown rendition profile: {name!r} (have: {', '.join(sorted(PROFILES))}, page:<n>)"
    )


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


def sniff_image_mime(source: str | Path) -> str | None:
    """Identify a file's image type from its leading bytes.

    Args:
        source: Path to the file to inspect.

    Returns:
        The MIME type, or ``None`` for anything that is not a recognised raster
        image. A caller deciding whether to *accept* bytes must use this rather
        than the filename: the two disagree exactly when it matters.
    """
    with Path(source).expanduser().open("rb") as handle:
        head = handle.read(_SNIFF_BYTES)
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    for signature, mime in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return mime
    return None


def check_image_pixel_budget(
    source: str | Path, *, limit: int = MAX_IMAGE_PIXELS
) -> tuple[int, int]:
    """Read an image's dimensions from its header and refuse a decompression bomb.

    ``Image.open`` parses the header only, so this costs nothing next to the
    decode it is there to prevent.

    Args:
        source: Path to the image file.
        limit: Maximum pixel count to allow.

    Returns:
        The image's ``(width, height)``.

    Raises:
        ImageTooLarge: If ``width * height`` exceeds ``limit``, or if Pillow
            refuses the header outright as a bomb.
        UnsupportedRendition: If the file is not an image Pillow can read.
    """
    try:
        with Image.open(Path(source).expanduser()) as image:
            width, height = image.size
    except Image.DecompressionBombError as exc:
        raise ImageTooLarge(f"image refused as a decompression bomb: {exc}") from exc
    except UnidentifiedImageError as exc:
        raise UnsupportedRendition(f"not a raster image Pillow can read: {source}") from exc
    if width * height > limit:
        raise ImageTooLarge(
            f"image is {width}×{height} ({width * height} pixels); the limit is {limit}"
        )
    return width, height


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


def _readable_hashes(conn: sqlite3.Connection, store: Store) -> frozenset[str] | None:
    """The asset hashes this principal's describing nodes reach; ``None`` = all.

    A human gets ``None`` — unfiltered, like every other read. An agent gets
    the hashes carried by the live ``asset_ref`` nodes inside its read set,
    which is what "an asset is as reachable as its describing nodes" means in
    SQL. An agent with no grants therefore reaches nothing at all, without a
    separate check for that case.

    **A ``proposed`` description counts.** Everywhere else in this system a
    proposed node is *readable* and merely filtered out of search at query
    time (``search`` defaults to ``state="active"``, and the FTS projector
    indexes every state) — so requiring ``active`` here would have made assets
    the one read in the graph with a stricter rule than the nodes describing
    them, and :func:`_resolve_hash` already admitted proposed rows on its
    node-id branch, leaving the two halves of one lookup disagreeing.

    It also has to count for ingestion to work at all: an agent holding
    ``suggest`` lands its describing node ``proposed``, and under the stricter
    reading it could not re-read the bytes it had just ingested — page rasters
    of its own PDF included — until a human accepted.

    The residual is an existence oracle, and a thin one: proposing an
    ``asset_ref`` for a hash reveals whether those bytes are already stored.
    Reaching them needs the sha256, which is unguessable and which a caller
    can only compute from bytes it already holds — and content-addressed
    registration answers the same question anyway by deduplicating.
    """
    if store.principal.read_spaces is None:
        return None
    scope, params = store.node_scope()
    rows = conn.execute(
        "SELECT DISTINCT json_extract(props,'$.asset_hash') AS hash FROM nodes"
        f" WHERE type_id = 'asset_ref' AND state != 'archived'{scope}",
        params,
    ).fetchall()
    return frozenset(row["hash"] for row in rows if row["hash"])


def _resolve_hash(conn: sqlite3.Connection, id_or_hash: str, principal: Principal) -> str:
    """Resolve a readable asset's hash, directly or through a describing node's id.

    Both branches are scoped, so neither a hash nor a node id tells the caller
    about an asset it may not reach: an unreachable one answers *not found*.

    Raises:
        AssetNotFound: If nothing readable resolves.
    """
    store = Store(conn, principal)
    readable = _readable_hashes(conn, store)
    row = conn.execute("SELECT hash FROM assets WHERE hash = ?", (id_or_hash,)).fetchone()
    if row is not None and (readable is None or row["hash"] in readable):
        return row["hash"]
    scope, params = store.node_scope()
    node = conn.execute(
        f"SELECT props FROM nodes WHERE id = ? AND state != 'archived'{scope}",
        (id_or_hash, *params),
    ).fetchone()
    if node is not None:
        asset_hash = json.loads(node["props"]).get("asset_hash")
        if (
            asset_hash
            and (readable is None or asset_hash in readable)
            and conn.execute("SELECT 1 FROM assets WHERE hash = ?", (asset_hash,)).fetchone()
        ):
            return asset_hash
    raise AssetNotFound(f"asset not found: {id_or_hash}")


def get_asset(id_or_hash: str, *, principal: Principal, path: str | Path | None = None) -> AssetOut:
    """Fetch one asset's metadata by hash or by an asset-reference node's id.

    Raises:
        AssetNotFound: If no asset the principal can reach resolves — through
            a readable ``asset_ref`` node, per the module's access note.
    """
    conn = _connect(path)
    try:
        asset_hash = _resolve_hash(conn, id_or_hash, principal)
        row = conn.execute("SELECT * FROM assets WHERE hash = ?", (asset_hash,)).fetchone()
        return _asset_out(row)
    finally:
        conn.close()


def set_extracted_text(
    asset_hash: str, text: str | None, *, path: str | Path | None = None
) -> None:
    """Store (or clear) the text an extraction handler pulled out of an asset.

    Takes no principal and writes no event, for the same reason registration
    does neither (migration ``0007``'s comment): an asset row is
    content-addressed base state, not graph state. There is nothing to undo —
    the same bytes always resolve to the same row, and re-extracting them lands
    the same text — and there is nobody to authorise against here, because
    access to an asset is decided one level up, by the ``asset_ref`` nodes that
    describe the hash (see the module docstring's access note). The ingestion
    pipeline that calls this emits the single ``asset.ingest`` event covering
    the whole run, of which the extraction is one step; a second event would
    only say the same thing again, with bytes-derived text in its payload.

    Args:
        asset_hash: The asset's sha256.
        text: The extracted text, or ``None`` to clear it (an extraction that
            produced nothing is not the same as one that never ran).
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Raises:
        AssetNotFound: If no asset row carries that hash.
    """
    conn = _connect(path)
    try:
        cursor = conn.execute(
            "UPDATE assets SET extracted_text = ? WHERE hash = ?", (text, asset_hash)
        )
        if cursor.rowcount == 0:
            raise AssetNotFound(f"asset not found: {asset_hash}")
        conn.commit()
    finally:
        conn.close()


def list_assets(*, principal: Principal, path: str | Path | None = None) -> list[AssetOut]:
    """List the assets this principal can reach, in registration order."""
    conn = _connect(path)
    try:
        rows = conn.execute("SELECT * FROM assets ORDER BY created_at, hash").fetchall()
        readable = _readable_hashes(conn, Store(conn, principal))
        return [_asset_out(row) for row in rows if readable is None or row["hash"] in readable]
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

    Pillow reads through the blob handle, so only the decoded file — not the
    stored bytes — is ever held in memory. The pixel budget is checked from the
    header first: the HTTP surface refuses a bomb at upload, but an asset
    registered through the CLI (or before that check existed) reaches here
    unfiltered, and the decode is what costs the memory.

    Raises:
        ImageTooLarge: If the stored image is above :data:`MAX_IMAGE_PIXELS`.
    """
    with Image.open(io.BufferedReader(_BlobReader(original))) as image:
        width, height = image.size
        if width * height > MAX_IMAGE_PIXELS:
            raise ImageTooLarge(
                f"stored image is {width}×{height} ({width * height} pixels); "
                f"the rendition limit is {MAX_IMAGE_PIXELS} — purge it or keep it outside the graph"
            )
        transposed = ImageOps.exif_transpose(image)
        if transposed.mode not in ("RGB", "RGBA"):
            has_alpha = "A" in transposed.getbands() or "transparency" in transposed.info
            transposed = transposed.convert("RGBA" if has_alpha else "RGB")
        return _downscale(transposed, profile)


def _downscale(image: Image.Image, profile: Profile) -> Image.Image:
    """Shrink an image to the profile's max edge, in place, and return it.

    ``thumbnail()`` only ever shrinks, so an image already inside the cap keeps
    its size: no rendition is ever an upscale of what it was derived from. Both
    render paths — stored image and PDF page — end here, so the "never upscale"
    rule has one home.
    """
    image.thumbnail((profile.max_edge, profile.max_edge), Image.LANCZOS)
    return image


def _render_pdf_page(original: sqlite3.Blob, page_number: int, profile: Profile) -> Image.Image:
    """Rasterise one page of a stored PDF and return the downscaled image.

    ``pypdfium2`` is imported here rather than at module scope so that an
    install without the ``pdf`` extra still starts, still serves ``thumb`` and
    ``preview``, and only fails — with a message naming the extra — on the one
    request it genuinely cannot serve.

    PDFium accepts any object with ``seek``/``tell``/``read``/``readinto`` and
    reads the file on demand, so the PDF is streamed straight out of its blob
    through :class:`_BlobReader`: a 200 MB scan is never held in memory whole,
    and only the pages actually asked for are parsed.

    Args:
        original: Open blob handle on the stored PDF's bytes.
        page_number: 1-based page to render.
        profile: Geometry and encoding spec (:data:`PAGE_PROFILE`).

    Returns:
        The rendered page, downscaled to the profile's cap.

    Raises:
        UnsupportedRendition: If ``pypdfium2`` is not installed, the page
            number is past the end of the document, or PDFium cannot parse
            the stored bytes as a PDF.
        ImageTooLarge: If the render would exceed :data:`MAX_IMAGE_PIXELS`.
    """
    try:
        import pypdfium2
    except ImportError as exc:
        raise UnsupportedRendition(
            f"cannot rasterise page {page_number}: pypdfium2 is not installed — "
            "install the 'pdf' extra (pip install 'nodum[pdf]') to render PDF pages"
        ) from exc

    scale = PAGE_DPI / 72  # PDFium scales the page's own 1/72-inch canvas unit.
    try:
        document = pypdfium2.PdfDocument(io.BufferedReader(_BlobReader(original)))
    except pypdfium2.PdfiumError as exc:
        raise UnsupportedRendition(f"cannot render this PDF: {exc}") from exc
    try:
        page_count = len(document)
        if page_number > page_count:
            raise UnsupportedRendition(
                f"page {page_number} is past the end of this PDF: it has {page_count} page(s)"
            )
        page = document[page_number - 1]
        width_points, height_points = page.get_size()
        width, height = round(width_points * scale), round(height_points * scale)
        # PDFium allocates the whole bitmap before it draws a single glyph, so
        # the pixel budget has to be spent up front, on arithmetic — there is no
        # header to read and no partial decode to abandon. A 200×200 inch page
        # (PDF's maximum) is 829 MP here, i.e. ~3.3 GB resident as RGBA.
        if width * height > MAX_IMAGE_PIXELS:
            raise ImageTooLarge(
                f"page {page_number} renders {width}×{height} ({width * height} pixels) "
                f"at {PAGE_DPI} DPI; the rendition limit is {MAX_IMAGE_PIXELS}"
            )
        image = _downscale(page.render(scale=scale).to_pil(), profile)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        # to_pil() can hand back a view onto PDFium's own buffer, which closing
        # the document frees; copy while it is still valid. Only the downscaled
        # image is copied, so the cost is bounded by the profile, not the page.
        return image.copy()
    finally:
        document.close()


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
    principal: Principal,
    path: str | Path | None = None,
) -> RenditionOut:
    """Fetch an image rendition, generating and caching it on first request.

    Args:
        id_or_hash: Asset hash, or the id of a node with an ``asset_hash`` prop.
        profile: ``thumb`` or ``preview`` for an image asset (see
            :data:`PROFILES`), or ``page:<n>`` for a 1-based page of a PDF.
        include_data: Embed the WebP bytes as base64 in the result (the MCP
            path); otherwise only metadata is returned and the stored bytes
            are never read.
        principal: The reader (see the module docstring's access note).
        path: Explicit database path; defaults to ``NODUM_DB`` resolution.

    Returns:
        The rendition metadata; ``cached`` reports whether this call hit the
        stored rendition or (re)generated the image.

    Raises:
        AssetNotFound: If no asset the principal can reach resolves.
        UnsupportedRendition: If the profile is unknown, the profile does not
            match the asset's kind (a page of a JPEG, a ``thumb`` of a PDF),
            the page is past the end of the document, ``pypdfium2`` is not
            installed, or the bytes are not something the renderer can read.
        ImageTooLarge: If rendering would exceed :data:`MAX_IMAGE_PIXELS`.
    """
    spec, page_number = resolve_profile(profile)

    conn = _connect(path)
    try:
        asset_hash = _resolve_hash(conn, id_or_hash, principal)
        asset = conn.execute("SELECT * FROM assets WHERE hash = ?", (asset_hash,)).fetchone()
        # Each profile family declares the one asset kind it can read, so a
        # mismatch says which kind was expected rather than "not an image".
        if page_number is not None:
            if asset["mime"] != "application/pdf":
                raise UnsupportedRendition(
                    f"page rasters are only supported for PDF assets, got {asset['mime']}"
                )
        elif not asset["mime"].startswith("image/"):
            raise UnsupportedRendition(
                f"the {profile!r} rendition is only supported for image assets, "
                f"got {asset['mime']} (a PDF renders through page:<n>)"
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
                if page_number is None:
                    image = _prepare_image(original, spec)
                else:
                    image = _render_pdf_page(original, page_number, spec)
        except UnidentifiedImageError as exc:
            raise UnsupportedRendition(
                f"cannot render {asset['mime']} ({asset['original_name']}): not a raster image"
            ) from exc
        except Image.DecompressionBombError as exc:
            # Pillow's own ceiling, which sits above ours: reached only if a
            # caller widened MAX_IMAGE_PIXELS past it. A 500 either way is
            # wrong — the stored image is the problem, not the server.
            raise ImageTooLarge(f"cannot render {asset['original_name']}: {exc}") from exc
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
