/**
 * A wikilink URL opened directly — route `/node/title/:title`.
 *
 * The app intercepts wikilink clicks and navigates by node id, but a middle
 * click, a bookmark, or a paste lands here with only the title. This view is
 * that entry point: it resolves the title the same way a click would and
 * redirects to `/node/:id` when it can, so a wikilink URL is not a dead end
 * and never resolves to a guess. An unresolvable title renders the same
 * sentence the click interceptor toasts, with a way out.
 */

import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { resolveTitles } from "../../api/client";
import { EmptyState, Spinner } from "../../components";
import { actionForResolution, describeFailure, wikilinkTargetId } from "../../lib";
import type { FailureDescription } from "../../lib";

type RedirectState =
  | { status: "resolving" }
  | { status: "unresolved"; toastTitle: string; toastDetail?: string }
  | { status: "failed"; failure: FailureDescription };

export default function NodeTitleRedirect() {
  const { title } = useParams<{ title: string }>();
  const navigate = useNavigate();
  const [state, setState] = useState<RedirectState>({ status: "resolving" });

  useEffect(() => {
    if (title === undefined) return;
    // An id-form wikilink URL (`/node/title/<id>`, from `[[<id>]]`) names
    // its node directly; the title resolve would answer "No active node
    // titled …" for an id, so redirect by id instead.
    const nodeId = wikilinkTargetId(title);
    if (nodeId !== null) {
      navigate(`/node/${nodeId}`, { replace: true });
      return;
    }
    const controller = new AbortController();
    setState({ status: "resolving" });
    resolveTitles([title], undefined, controller.signal)
      .then(([resolution]) => {
        if (controller.signal.aborted || resolution === undefined) return;
        const action = actionForResolution(resolution);
        if (action.kind === "navigate") {
          // `replace`: the title URL was a waypoint, not a page to come back to.
          navigate(`/node/${action.nodeId}`, { replace: true });
        } else {
          setState({ status: "unresolved", toastTitle: action.toastTitle, toastDetail: action.toastDetail });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({ status: "failed", failure: describeFailure(error, "that title") });
      });
    return () => controller.abort();
  }, [title, navigate]);

  return (
    <div className="nd-view">
      {state.status === "resolving" ? (
        <div className="nd-empty">
          <Spinner large label="Resolving title" />
        </div>
      ) : null}

      {state.status === "unresolved" ? (
        <EmptyState
          title={state.toastTitle}
          body={state.toastDetail ?? "Open search to find what the link pointed at."}
          action={
            <Link to="/search" className="nd-button">
              Search
            </Link>
          }
        />
      ) : null}

      {state.status === "failed" ? (
        <EmptyState
          title={state.failure.title}
          body={state.failure.body}
          action={
            <Link to="/search" className="nd-button">
              Search
            </Link>
          }
        />
      ) : null}
    </div>
  );
}
