"""Re-measure the embedding calibration fixture against the real model.

``tests/fixtures/embedding_calibration.json`` holds bilingual pairs labelled
into four bands by hand, with the cosine the pinned model gives each one. Those
cosines are a property of the model, the pooling fastembed applies to it, and
the chunking in :mod:`nodum.embeddings` — change any of the three and the bands
move, which is why this script exists.

**The two cosine bars in :mod:`nodum.consolidate` are NOT set from this file.**
They are the shipped values; bars derived from these bands alone were tried at
0.72/0.38 and reverted, because a set written to demonstrate a separation
cannot measure a false-positive rate — on real content 0.38 proposed 5.9
``relates_to`` edges per node. What this fixture is good for is detecting
*drift*: whether the model still scores its own labelled pairs where it used
to. Setting a bar needs a real corpus scored for volume and precision. See the
comments on :data:`nodum.consolidate.DUPLICATE_EMBEDDING_COSINE`.

Run it after such a change::

    uv run --extra embeddings python scripts/measure_embedding_calibration.py
    uv run --extra embeddings python scripts/measure_embedding_calibration.py --write

Without ``--write`` it only reports: the band table, where the current bars
fall inside it, and any pair whose cosine has drifted from the recorded value.
With ``--write`` it updates the recorded cosines in place. It deliberately does
not pick thresholds, and the refreshed band table is **not** a licence to
re-derive them from it — that is exactly the method that produced the reverted
pair.

Needs the ``embeddings`` extra and the model in the local cache (one-time
``NODUM_EMBED_DOWNLOAD=1``); it exits non-zero with the reason if either is
missing.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nodum import consolidate, embeddings  # noqa: E402

FIXTURE = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "embedding_calibration.json"
)

#: Report a recorded cosine as drifted past this much absolute difference.
DRIFT_TOLERANCE = 0.01

BAND_ORDER = ["duplicate", "related", "same_area", "unrelated"]


def cosine(first: list[float], second: list[float]) -> float:
    """Cosine similarity, matching :func:`nodum.consolidate._cosine`."""
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if not first_norm or not second_norm:
        return 0.0
    dot = sum(left * right for left, right in zip(first, second, strict=True))
    return dot / (first_norm * second_norm)


def measure(pairs: list[dict]) -> list[float]:
    """Cosine per pair, through the same call the consolidation cycle makes."""
    provider = embeddings.get_provider()
    if provider is None:
        raise SystemExit(f"no embedding provider: {embeddings.unavailable_reason()}")
    print(f"model: {provider.model_id} ({provider.dimensions} dims)")
    nodes = []
    for pair in pairs:
        nodes.append({"title": None, "content": pair["left"]})
        nodes.append({"title": None, "content": pair["right"]})
    vectors = embeddings.node_vectors(provider, nodes)
    return [cosine(vectors[2 * i], vectors[2 * i + 1]) for i in range(len(pairs))]


def main() -> int:
    """Measure, report, and optionally rewrite the fixture's cosines."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="update the recorded cosines in the fixture"
    )
    arguments = parser.parse_args()

    document = json.loads(FIXTURE.read_text())
    pairs = document["pairs"]
    measured = measure(pairs)

    drifted = [
        (pair, value)
        for pair, value in zip(pairs, measured, strict=True)
        if abs(value - pair["cosine"]) > DRIFT_TOLERANCE
    ]

    print(f"\n{'band':<12} {'n':>3} {'min':>8} {'max':>8}   {'verdict':<8}")
    bands: dict[str, list[float]] = {}
    for pair, value in zip(pairs, measured, strict=True):
        bands.setdefault(pair["band"], []).append(value)
    for band in BAND_ORDER:
        values = bands.get(band, [])
        if not values:
            continue
        duplicates = sum(1 for v in values if v >= consolidate.DUPLICATE_EMBEDDING_COSINE)
        links = sum(1 for v in values if v >= consolidate.LINK_EMBEDDING_COSINE)
        print(
            f"{band:<12} {len(values):>3} {min(values):>8.3f} {max(values):>8.3f}   "
            f"dup {duplicates}/{len(values)}  link {links}/{len(values)}"
        )

    print(
        f"\nbars in force: duplicate={consolidate.DUPLICATE_EMBEDDING_COSINE} "
        f"link={consolidate.LINK_EMBEDDING_COSINE}"
    )
    highest_non_duplicate = max(
        v for pair, v in zip(pairs, measured, strict=True) if pair["band"] != "duplicate"
    )
    weakest_duplicate = min(
        v for pair, v in zip(pairs, measured, strict=True) if pair["band"] == "duplicate"
    )
    above_negatives = consolidate.DUPLICATE_EMBEDDING_COSINE - highest_non_duplicate
    below_positives = weakest_duplicate - consolidate.DUPLICATE_EMBEDDING_COSINE
    # `below_positives` goes negative when the bar sits above the band it exists
    # to catch, which is exactly where the shipped bar is. The wording has to
    # follow the sign: written assuming the bar lands inside the gap, this line
    # rendered "--0.167 below the weakest duplicate" for a bar 0.167 above it.
    room = (
        f"{below_positives:.3f} below the weakest duplicate"
        if below_positives >= 0
        else f"{-below_positives:.3f} ABOVE the weakest duplicate, so it cannot fire"
    )
    print(
        f"  duplicate bar margins: {above_negatives:.3f} above the strongest non-duplicate, {room}"
    )

    if drifted:
        print(f"\n{len(drifted)} pair(s) drifted past {DRIFT_TOLERANCE}:")
        for pair, value in drifted:
            print(f"  {pair['id']:<26} recorded {pair['cosine']:>7.4f} -> measured {value:>7.4f}")
    else:
        print(f"\nno pair drifted past {DRIFT_TOLERANCE} — the recorded cosines still hold")

    if arguments.write:
        for pair, value in zip(pairs, measured, strict=True):
            pair["cosine"] = round(value, 4)
        FIXTURE.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        print(f"\nwrote {FIXTURE}")
        print(
            "this refreshes drift only — do not re-derive the bars from these bands; "
            "see their comments in nodum/consolidate.py"
        )
    return 1 if drifted and not arguments.write else 0


if __name__ == "__main__":
    raise SystemExit(main())
