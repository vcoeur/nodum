/**
 * The show-once token dialog.
 *
 * Agent creation and token rotation both answer with the token in the response
 * body — the one and only place it ever appears (HTTP has no stderr to print
 * it to, the way the CLI does). This dialog is the hand-off: the operator
 * copies the token into the agent's MCP config now, or it is unrecoverable and
 * the remedy is a rotation. There is deliberately no "show me later" path.
 */

import { useState } from "react";
import { Modal, useToast } from "../../components";

interface TokenDialogProps {
  /** The agent the token belongs to. */
  agentName: string;
  /** The token, fresh from the create or rotate response. */
  token: string;
  /** Close handler — dismissing forfeits the token. */
  onClose: () => void;
}

/** The show-once token hand-off. */
export function TokenDialog({ agentName, token, onClose }: TokenDialogProps) {
  const toast = useToast();
  const [copied, setCopied] = useState(false);

  const copy = () => {
    void navigator.clipboard.writeText(token).then(
      () => setCopied(true),
      () => toast.show("error", "Copy failed", "Select the token and copy it by hand."),
    );
  };

  return (
    <Modal title={`Token for ${agentName}`} onClose={onClose}>
      <p>
        This is the only time the token is shown — the server keeps its hash,
        not the token. Put it in the agent's MCP client configuration now, as{" "}
        <code>Authorization: Bearer …</code> against this server's{" "}
        <code>/mcp</code> URL; if it is lost, rotate the token.
      </p>
      <p className="nd-ad-token nd-mono">{token}</p>
      <div className="nd-row">
        <button type="button" className="nd-button nd-button--primary" onClick={copy}>
          {copied ? "Copied" : "Copy token"}
        </button>
      </div>
    </Modal>
  );
}
