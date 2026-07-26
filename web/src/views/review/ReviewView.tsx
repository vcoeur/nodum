/**
 * Route `/review` — the review queue.
 *
 * This is the safety-critical view. Everything it can do writes or retires live
 * state, which the service layer reserves to the `human` actor
 * (`_require_human_reviewer` over `HUMAN_ONLY_ACTIONS`, at the transition choke
 * point). The HTTP surface *is* the human surface: it forces `actor="human"`
 * server-side and exposes no request field that sets one, and the typed client
 * has no actor parameter anywhere. There is therefore nothing in this view that
 * chooses a reviewer identity — no picker, no setting, no header. If that ever
 * looks like a missing feature, it is the guarantee working.
 */

import { ReviewInbox } from "./ReviewInbox";
import "./review.css";

/** The review route. */
export default function ReviewView() {
  return (
    <div className="nd-view nd-rv">
      <header className="nd-view__header">
        <div>
          <h1>Review</h1>
          <p className="nd-meta nd-rv__subtitle">
            Proposals waiting for a human, grouped by space and then by agent.
            Accepting or rejecting one writes live state and is recorded as
            actor human. A space whose agents hold <code>edit</code> writes
            straight to <code>active</code> and never appears here — it is
            listed at the bottom so its silence is legible rather than
            invisible.
          </p>
        </div>
      </header>

      <ReviewInbox />
    </div>
  );
}

export { ReviewView };
