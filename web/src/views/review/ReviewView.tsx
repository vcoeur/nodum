/**
 * Route `/review` — the review queue and the policy editor.
 *
 * This is the safety-critical view. Everything it can do writes or retires live
 * state, which the service layer reserves to the `human` actor
 * (`_require_human_reviewer` over `HUMAN_ONLY_ACTIONS`, at the transition choke
 * point). The HTTP surface *is* the human surface: it forces `actor="human"`
 * server-side and exposes no request field that sets one, and the typed client
 * has no actor parameter anywhere. There is therefore nothing in this view that
 * chooses a reviewer identity — no picker, no setting, no header. If that ever
 * looks like a missing feature, it is the guarantee working.
 *
 * Two surfaces, one page, because they are the same decision at two time
 * scales: the queue decides one proposal now, a policy decides every matching
 * proposal in advance. Design §9's per-job autonomy dial is the `job`-rule half
 * of the policy editor rather than a third tab.
 */

import { useState } from "react";
import { ReviewInbox } from "./ReviewInbox";
import { PolicyEditor } from "./PolicyEditor";
import "./review.css";

/** The two panels this route holds. */
type Panel = "queue" | "policies";

/** The review route. */
export default function ReviewView() {
  const [panel, setPanel] = useState<Panel>("queue");

  return (
    <div className="nd-view nd-rv">
      <header className="nd-view__header">
        <div>
          <h1>Review</h1>
          <p className="nd-meta nd-rv__subtitle">
            {panel === "queue"
              ? "Proposals waiting for a human. Accepting or rejecting one writes live state and is recorded as actor human."
              : "Policies decide in advance what an agent may write live without ever reaching this queue."}
          </p>
        </div>

        <nav className="nd-rv__tabs" aria-label="Review panels">
          <button
            type="button"
            className={panel === "queue" ? "nd-rv__tab nd-rv__tab--active" : "nd-rv__tab"}
            onClick={() => setPanel("queue")}
            aria-current={panel === "queue"}
          >
            Queue
          </button>
          <button
            type="button"
            className={panel === "policies" ? "nd-rv__tab nd-rv__tab--active" : "nd-rv__tab"}
            onClick={() => setPanel("policies")}
            aria-current={panel === "policies"}
          >
            Policies
          </button>
        </nav>
      </header>

      {panel === "queue" ? <ReviewInbox /> : <PolicyEditor />}
    </div>
  );
}

export { ReviewView };
