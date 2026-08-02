"""Measure the two cosine bars on real content — the numbers they are set from.

``tests/fixtures/embedding_calibration.json`` cannot set a bar: its 29
hand-labelled pairs were written to demonstrate a separation, and a separation
is not a false-positive rate (the 0.72/0.38 pair derived from it was tried on
real content and reverted). This script is the real-corpus measurement the
replacement came from — the numbers it prints are what the current bars in
:mod:`nodum.consolidate` were chosen from, and re-running it is how a future
re-tuning starts (change the model, fastembed's pooling, or ``CHUNK_WORDS`` and
the tables move).

It loads the kasten vault's prose (``note/`` + ``literature/``, frontmatter
and wikilinks stripped, at least 300 characters), samples 200 notes, embeds
them through :func:`nodum.embeddings.node_vectors` — the same call the
consolidation cycle makes — and prints two tables:

* **volume** — ``relates_to`` proposals per node (pairs at or above the bar,
  divided by the number of sampled nodes) at 0.38/0.55/0.60/0.65/0.80;
* **precision** — against the vault's own wikilinks as ground truth (two notes
  are related iff one links the other by title/stem), at
  0.45/0.50/0.55/0.60/0.65/0.70.

On the corpus this measures, the link bar at 0.60 fires at about 1.2
``relates_to`` proposals per node with ~10 % precision by wikilink ground
truth, 0.80 measures dead (0.04 per node) and the reverted 0.38 measures as a
flood (5.9-6.4 per node) — those are the numbers the current bars were chosen
from. The vault is a live corpus, so re-running today lands close to but not
exactly on them; the fixed seed is what makes a re-run's change from *yours*
model drift rather than sampling noise, and a future re-tuning starts from
whatever the tables say then.

Run it with::

    uv run --extra embeddings python scripts/measure_kasten_calibration.py
    uv run --extra embeddings python scripts/measure_kasten_calibration.py /path/to/kasten

It needs the ``embeddings`` extra and the model in the local cache; it exits
non-zero with the reason if either is missing. 200 sampled notes keep a run to
about a minute — the whole vault is deliberately never embedded by default.
"""

from __future__ import annotations

import argparse
import math
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nodum import consolidate, embeddings  # noqa: E402

DEFAULT_VAULT = Path("~/src/vcoeur/knoten/kasten").expanduser()

#: A note must keep this much prose after stripping before it counts as part of
#: the corpus — anything shorter is a stub, a pointer, or a bare quote.
MIN_CHARS = 300

#: How many notes are sampled for the measurement. 200 keeps a run to about a
#: minute; the whole vault is never embedded by default.
SAMPLE_SIZE = 200

#: Fixed seed: a re-run must reproduce the same sample, or a measured change
#: could be sampling noise rather than the model drifting.
SAMPLE_SEED = 20260802

#: Bars the volume table reports (``relates_to`` proposals per node).
VOLUME_BARS = [0.38, 0.55, 0.60, 0.65, 0.80]

#: Bars the precision table reports (against the vault's wikilinks).
PRECISION_BARS = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70]

_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[.*?\]\]")
_MARKER_PREFIX_RE = re.compile(r"^[^\w]+")


def _normalise(text: str) -> str:
    """The comparison form of a title or link target: casefolded, collapsed."""
    return " ".join(text.casefold().split())


def _title(stem: str) -> str:
    """A note's title from its filename, the knoten marker (``! ``) stripped.

    A link target carries the marker when it names a ``!``-prefixed note, so
    both sides of a match go through the same strip.
    """
    return _normalise(_MARKER_PREFIX_RE.sub("", stem).strip())


def _wikilink_targets(text: str) -> list[str]:
    """The targets of every wikilink in a note, display aliases dropped."""
    return [
        _MARKER_PREFIX_RE.sub("", link[2:-2].strip().split("|")[0]).strip()
        for link in _WIKILINK_RE.findall(text)
    ]


def load_notes(vault: Path) -> list[dict]:
    """Every prose note in the vault: text for embedding, links for ground truth.

    ``note/`` and ``literature/`` only — the other trees (``entity/``,
    ``files/``, ``journal/``) are not prose notes. Frontmatter and wikilinks
    are stripped from the text that gets embedded; the wikilink *targets* are
    read from the raw file because they are the precision ground truth.
    """
    notes: list[dict] = []
    for tree in ("note", "literature"):
        directory = vault / tree
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            # "Wikilinks stripped" means the `[[ ]]` markup removed and the
            # label kept: a link names another note, and its words are the
            # note's prose. Removing the label too would strip real content.
            content = _WIKILINK_RE.sub(
                lambda match: match.group(0)[2:-2], _FRONTMATTER_RE.sub("", raw)
            ).strip()
            if len(content) < MIN_CHARS:
                continue
            notes.append(
                {
                    "title": _title(path.stem),
                    "content": content,
                    "targets": {_normalise(target) for target in _wikilink_targets(raw)},
                }
            )
    return notes


def related_pairs(notes: list[dict]) -> set[tuple[str, str]]:
    """The vault's own wikilinks as ground truth: A relates B iff one links the other.

    Under-counts by construction — a target naming a note outside the sample,
    or an alias the title does not match, is a related pair this set cannot
    see — which is the direction the precision table errs in.
    """
    by_title = {note["title"]: note for note in notes}
    related: set[tuple[str, str]] = set()
    for note in notes:
        for target in note["targets"]:
            other = by_title.get(target)
            if other is None:
                continue
            related.add(tuple(sorted((note["title"], other["title"]))))
    return related


def cosine(first: list[float], second: list[float]) -> float:
    """Cosine similarity, matching :func:`nodum.consolidate._cosine`."""
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if not first_norm or not second_norm:
        return 0.0
    dot = sum(left * right for left, right in zip(first, second, strict=True))
    return dot / (first_norm * second_norm)


def main() -> int:
    """Sample, embed, and print the volume and precision tables."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "vault",
        nargs="?",
        default=str(DEFAULT_VAULT),
        help="the kasten vault to measure (default: %(default)s)",
    )
    arguments = parser.parse_args()
    vault = Path(arguments.vault).expanduser()
    if not vault.is_dir():
        raise SystemExit(f"kasten vault not found: {vault}")

    provider = embeddings.get_provider()
    if provider is None:
        raise SystemExit(f"no embedding provider: {embeddings.unavailable_reason()}")

    notes = load_notes(vault)
    print(f"model: {provider.model_id} ({provider.dimensions} dims)")
    print(
        f"corpus: {len(notes)} prose notes (>= {MIN_CHARS} chars, frontmatter "
        "and wikilinks stripped)"
    )

    sample = random.Random(SAMPLE_SEED).sample(notes, min(SAMPLE_SIZE, len(notes)))
    print(
        f"sample: {len(sample)} notes, {len(sample) * (len(sample) - 1) // 2} pairs "
        f"(seed {SAMPLE_SEED})"
    )

    vectors = embeddings.node_vectors(
        provider, [{"title": None, "content": note["content"]} for note in sample]
    )
    pair_cosines: list[tuple[tuple[str, str], float]] = []
    for first_index in range(len(sample)):
        for second_index in range(first_index + 1, len(sample)):
            pair = tuple(sorted((sample[first_index]["title"], sample[second_index]["title"])))
            pair_cosines.append((pair, cosine(vectors[first_index], vectors[second_index])))

    print(f"\nvolume — relates_to proposals per node (pairs >= bar / {len(sample)} nodes):")
    print(f"{'bar':<6} {'pairs':>7} {'per node':>9}")
    for bar in VOLUME_BARS:
        count = sum(1 for _, value in pair_cosines if value >= bar)
        print(f"{bar:<6} {count:>7} {count / len(sample):>9.2f}")

    related = related_pairs(sample)
    print(
        "\nprecision — wikilink ground truth (related pairs above the bar / pairs above the bar):"
    )
    print(f"{'bar':<6} {'above':>7} {'true':>6} {'precision':>10}")
    for bar in PRECISION_BARS:
        above = [pair for pair, value in pair_cosines if value >= bar]
        true = sum(1 for pair in above if pair in related)
        precision = true / len(above) if above else 0.0
        print(f"{bar:<6} {len(above):>7} {true:>6} {precision:>10.1%}")

    print(
        f"\nbars in force: duplicate={consolidate.DUPLICATE_EMBEDDING_COSINE} "
        f"link={consolidate.LINK_EMBEDDING_COSINE}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
