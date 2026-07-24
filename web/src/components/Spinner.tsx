/** Indeterminate loading indicator. */

interface SpinnerProps {
  /** Larger variant, for a whole-view load rather than an inline one. */
  large?: boolean;
  /** Announced to screen readers; also the `title` on hover. */
  label?: string;
}

/**
 * An indeterminate spinner.
 *
 * @param large Use the larger size for a full-view load.
 * @param label Accessible label; defaults to "Loading".
 */
export function Spinner({ large = false, label = "Loading" }: SpinnerProps) {
  return (
    <span
      className={large ? "nd-spinner nd-spinner--large" : "nd-spinner"}
      role="status"
      aria-label={label}
      title={label}
    />
  );
}
