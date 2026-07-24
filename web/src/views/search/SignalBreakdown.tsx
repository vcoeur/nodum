import { SIGNAL_HELP, SIGNAL_KEYS, SIGNAL_LABEL, formatSignalValue } from "./signals";
import type { HitSignals, SignalKey } from "./signals";

/**
 * The per-hit "why did this match?" strip.
 *
 * The design problem: three floats on three different scales explain nothing at
 * a glance. What a person actually wants to know, in order, is *which* signals
 * fired, *how strongly relative to each other*, and *how far up each signal's
 * own list* the hit sat. So the strip renders, left to right:
 *
 * 1. a two-segment share bar — the fusion, to scale, since `bm25 + vector`
 *    equals the fused score exactly;
 * 2. one chip per signal carrying the recovered rank (`keyword #1`), which is
 *    the number a human can reason about, with the raw contribution on the
 *    tooltip for when they cannot;
 * 3. the fused score itself, in mono, last — present for verification, not for
 *    scanning.
 *
 * A neighbour (graph-expansion) hit gets none of that. It did not match; it was
 * pulled in next to something that did, and its number is an edge weight rather
 * than a fusion contribution. Giving it a share bar would invite exactly the
 * wrong comparison, so it gets one chip and a different colour instead.
 *
 * The bar is `aria-hidden`: the chips carry the same information as text.
 */

interface SignalBreakdownProps {
  /** The hit's signals, as read by `describeSignals`. */
  signals: HitSignals;
  /** The hit's fused score, shown verbatim. */
  score: number;
}

/**
 * Render one hit's signal breakdown.
 *
 * @param signals Per-signal parts, dominance, and neighbour/corroboration flags.
 * @param score The server's fused score for the hit.
 */
export function SignalBreakdown({ signals, score }: SignalBreakdownProps) {
  const shareParts = signals.parts.filter((part) => part.share !== null);

  return (
    <div className="nd-search-signals">
      {shareParts.length > 0 ? (
        <span className="nd-search-signals__bar" aria-hidden="true">
          {shareParts.map((part) => (
            <span
              key={part.key}
              className={`nd-search-signals__segment nd-search-signals__segment--${part.key}`}
              style={{ flexGrow: part.share ?? 0 }}
            />
          ))}
        </span>
      ) : null}

      <span className="nd-search-signals__chips">
        {signals.parts.map((part) => (
          <span
            key={part.key}
            className={`nd-search-signals__chip nd-search-signals__chip--${part.key}`}
            title={`${SIGNAL_HELP[part.key]} Contribution ${formatSignalValue(part.value)}${
              part.share === null ? "" : ` (${Math.round(part.share * 100)}% of the fused score)`
            }`}
          >
            <span className="nd-search-signals__dot" aria-hidden="true" />
            {part.label}
            {part.rank === null ? null : (
              <span className="nd-search-signals__rank">#{part.rank}</span>
            )}
            {part.key === "graph" ? (
              <span className="nd-search-signals__rank">{formatSignalValue(part.value)}</span>
            ) : null}
          </span>
        ))}

        {signals.unknown.map((extra) => (
          <span key={extra.key} className="nd-search-signals__chip" title="Unrecognised signal">
            {extra.key}
            <span className="nd-search-signals__rank">{formatSignalValue(extra.value)}</span>
          </span>
        ))}

        {signals.isCorroborated ? (
          <span
            className="nd-search-signals__agree"
            title="Both retrieval signals ranked this hit — the words and the meaning agree."
          >
            both
          </span>
        ) : null}
      </span>

      <span
        className="nd-search-signals__score nd-mono"
        title={
          signals.isNeighbour
            ? "Edge weight (edge-type weight × confidence), not a fused score."
            : "Fused score: the sum of the reciprocal-rank-fusion contributions."
        }
      >
        {formatSignalValue(score)}
      </span>
    </div>
  );
}

/**
 * The legend that makes the bar colours mean something, shown once above the list.
 *
 * @param neighbours Whether to include the `graph` swatch — it is only
 *   meaningful when expansion is on, and showing a colour that cannot appear
 *   would be noise.
 */
export function SignalLegend({ neighbours }: { neighbours: boolean }) {
  const keys: readonly SignalKey[] = neighbours
    ? SIGNAL_KEYS
    : SIGNAL_KEYS.filter((key) => key !== "graph");

  return (
    <span className="nd-search-legend">
      {keys.map((key) => (
        <span key={key} className="nd-search-legend__item" title={SIGNAL_HELP[key]}>
          <span className={`nd-search-legend__dot nd-search-legend__dot--${key}`} aria-hidden="true" />
          {SIGNAL_LABEL[key]}
        </span>
      ))}
    </span>
  );
}
