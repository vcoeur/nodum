/**
 * Route `/settings` — the configuration surface.
 *
 * Every SP2 capability in the browser: grouped rows with the effective value,
 * the layer it came from and its default; environment-pinned rows disabled
 * with the reason (the API's 409 backs the disabled state if a stale client
 * bypasses it); saves that report what actually happened live, next run, or
 * within a minute; adopt-from-environment with a preview; one-click revert
 * from each key's last settings event.
 *
 * The deciding logic lives in `settingsModel.ts` — this component is wiring,
 * which is all the component-less unit harness can verify anyway. The page is
 * honest about two things the copy must not blur: an adopted key keeps
 * resolving `provenance: "environment"` (adopt stores the same value in the
 * file without touching the host), and lowering a budget never stops a cycle
 * already spending — that is what the journal's stop button is for, so the
 * Gardener group links there by URL like every other cross-view reference.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { EmptyState, Modal, Spinner, useToast } from "../../components";
import { api } from "../../api/client";
import type { EventOut, SettingAdoptOut, SettingOut, SettingsOut } from "../../api/types";
import { describeFailure } from "../../lib";
import type { FailureDescription } from "../../lib";
import {
  EMBED_DOWNLOAD_NOTE,
  EMBED_MODEL_KEY,
  EXPORT_FILENAME,
  GROUPS,
  PROFILE_DEFAULT_NOTE,
  WITH_KEYS_EXPORT_CONFIRM,
  adoptPreview,
  editBlocker,
  isModelChange,
  layerLabel,
  liveness,
  livenessLabel,
  modelChangeConfirm,
  revertTarget,
} from "./settingsModel";
import "./settings.css";

/** How many log events the revert column reads; settings events are rare. */
const EVENT_WINDOW = 200;

/** The model write held behind the consequence confirm — a save or a revert. */
type PendingModelWrite =
  | { kind: "save"; draft: string }
  | { kind: "revert"; value: string | null };

/** The newest settings event per key, read once per load. */
function lastSettingsEventBy(events: readonly EventOut[]): Map<string, EventOut> {
  const latest = new Map<string, EventOut>();
  for (const event of events) {
    if (!event.op.startsWith("settings.")) continue;
    const key = event.payload.key;
    if (typeof key === "string" && !latest.has(key)) latest.set(key, event);
  }
  return latest;
}

/** A row's effective value as the page renders it — secrets show state only. */
function valueText(row: SettingOut): string {
  if (!row.set) return "not set";
  return row.secret ? "set" : (row.value ?? "");
}

/** The Settings route. */
export default function SettingsView() {
  const toast = useToast();
  const [report, setReport] = useState<SettingsOut | null>(null);
  const [events, setEvents] = useState<EventOut[]>([]);
  const [failure, setFailure] = useState<FailureDescription | null>(null);
  /** Draft text per editable key, only while it differs from what is in force. */
  const [drafts, setDrafts] = useState<Map<string, string>>(new Map());
  /** Per-key note from the last completed action on this page visit. */
  const [notes, setNotes] = useState<Map<string, string>>(new Map());
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [revertingKey, setRevertingKey] = useState<string | null>(null);
  const [adoptOpen, setAdoptOpen] = useState(false);
  const [adopting, setAdopting] = useState(false);
  /** The with-keys export dialog: open flag, the password field, in-flight flag. */
  const [exportOpen, setExportOpen] = useState(false);
  const [exportPassword, setExportPassword] = useState("");
  const [exporting, setExporting] = useState(false);
  /** The model-change confirm: the write is held until the consequence is named. */
  const [pendingModel, setPendingModel] = useState<PendingModelWrite | null>(null);
  /** The mixed-model banner's rebuild, in flight. */
  const [rebuilding, setRebuilding] = useState(false);

  /** Fetch both reads and put them in state; throws, so callers decide the failure copy. */
  const fetchData = useCallback(async () => {
    const [settingsReport, eventPage] = await Promise.all([
      api.getSettings(),
      api.listEvents(EVENT_WINDOW),
    ]);
    setReport(settingsReport);
    setEvents(eventPage);
  }, []);

  /** Re-read after a mutation: a failure here is a load failure, not a failed write. */
  const refresh = useCallback(async () => {
    try {
      await fetchData();
    } catch (error) {
      setFailure(describeFailure(error, "the settings"));
    }
  }, [fetchData]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        await fetchData();
      } catch (error) {
        if (!cancelled) setFailure(describeFailure(error, "the settings"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchData]);

  const rowsByKey = useMemo(
    () => new Map((report?.settings ?? []).map((row) => [row.key, row])),
    [report],
  );
  const eventsByKey = useMemo(() => lastSettingsEventBy(events), [events]);
  const capabilities = report?.capabilities ?? {};
  const adoptCandidates = useMemo(
    () => (report ? adoptPreview(report.settings).candidates : []),
    [report],
  );

  const setDraft = (key: string, value: string) => {
    setDrafts((previous) => {
      const next = new Map(previous);
      next.set(key, value);
      return next;
    });
  };
  const clearDraft = (key: string) => {
    setDrafts((previous) => {
      if (!previous.has(key)) return previous;
      const next = new Map(previous);
      next.delete(key);
      return next;
    });
  };
  const setNote = (key: string, note: string) => {
    setNotes((previous) => {
      const next = new Map(previous);
      next.set(key, note);
      return next;
    });
  };

  const saveRow = async (row: SettingOut) => {
    const draft = drafts.get(row.key);
    if (draft === undefined || savingKey !== null) return;
    // A model change blinds every stored chunk until the rebuild re-embeds
    // them — that consequence is confirmed before the write, never after.
    if (isModelChange(row.key)) {
      setPendingModel({ kind: "save", draft });
      return;
    }
    await writeRow(row, draft);
  };

  const writeRow = async (row: SettingOut, draft: string) => {
    setSavingKey(row.key);
    try {
      await api.applySettings({ [row.key]: draft });
    } catch (error) {
      // The seam's own sentence is the message: unknown name, bad value,
      // or — for a stale client on a pinned row — SettingPinned's pin
      // sentence behind the 409.
      toast.showError(error, `${row.key} was not stored`);
      return;
    } finally {
      setSavingKey(null);
    }
    clearDraft(row.key);
    setNote(row.key, livenessLabel(liveness(row.key)));
    await refresh();
  };

  /** Confirm the pending model write — a save or a revert; both change the model. */
  const confirmModelChange = async () => {
    const pending = pendingModel;
    setPendingModel(null);
    const row = rowsByKey.get(EMBED_MODEL_KEY);
    if (row === undefined || pending === null) return;
    if (pending.kind === "save") {
      await writeRow(row, pending.draft);
    } else {
      await performRevert(row, pending.value);
    }
  };

  /** The mixed-model banner's offered action: re-embed everything with the current model. */
  const rebuildVectors = async () => {
    if (rebuilding) return;
    setRebuilding(true);
    try {
      const run = await api.rebuildProjector("vec");
      toast.show(
        "success",
        "Vector index rebuilt",
        `${run.applied} event${run.applied === 1 ? "" : "s"} re-embedded with the current model.`,
      );
    } catch (error) {
      toast.showError(error, "The vector index was not rebuilt");
    } finally {
      setRebuilding(false);
    }
    await refresh();
  };

  const performRevert = async (row: SettingOut, value: string | null) => {
    setRevertingKey(row.key);
    try {
      if (value === null) {
        await api.unsetSetting(row.key);
      } else {
        await api.applySettings({ [row.key]: value });
      }
    } catch (error) {
      toast.showError(error, `${row.key} was not reverted`);
      return;
    } finally {
      setRevertingKey(null);
    }
    setNote(row.key, `Reverted to the previous value${value === null ? " (removed)" : ""}`);
    await refresh();
  };

  const revertRow = async (row: SettingOut) => {
    const target = revertTarget(row, eventsByKey.get(row.key) ?? null, capabilities);
    if (target === null || revertingKey !== null) return;
    // Reverting the model is a model change: it gets the same confirm, with
    // the same consequence, as a save does.
    if (isModelChange(row.key)) {
      setPendingModel({
        kind: "revert",
        value: target.kind === "write" ? target.value : null,
      });
      return;
    }
    await performRevert(row, target.kind === "write" ? target.value : null);
  };

  const adopt = async () => {
    setAdopting(true);
    let result: SettingAdoptOut;
    try {
      result = await api.adoptEnvironment();
    } catch (error) {
      toast.showError(error, "Nothing was adopted");
      return;
    } finally {
      setAdopting(false);
      setAdoptOpen(false);
    }
    const skipped = result.skipped.map((skip) => skip.key).join(", ");
    toast.show(
      "success",
      `Adopted ${result.count} setting${result.count === 1 ? "" : "s"} from the environment`,
      skipped === "" ? undefined : `Refused values: ${skipped}`,
    );
    await refresh();
  };

  /** Hand a response blob to the browser's save flow; nothing else controls where it lands. */
  const saveDownload = (blob: Blob) => {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = EXPORT_FILENAME;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const exportRedacted = async () => {
    if (exporting) return;
    setExporting(true);
    try {
      saveDownload(await api.exportSettings({ includeSecrets: false }));
      toast.show(
        "success",
        "Redacted export downloaded",
        `Saved as ${EXPORT_FILENAME} in your browser's download location.`,
      );
    } catch (error) {
      toast.showError(error, "Nothing was downloaded");
    } finally {
      setExporting(false);
    }
  };

  const exportWithKeys = async () => {
    if (exporting || exportPassword === "") return;
    setExporting(true);
    try {
      saveDownload(await api.exportSettings({ includeSecrets: true, password: exportPassword }));
      toast.show(
        "success",
        "Export with API key downloaded",
        `The file holds your real key. Saved as ${EXPORT_FILENAME} in your browser's download location.`,
      );
      setExportOpen(false);
      setExportPassword("");
    } catch (error) {
      // A wrong step-up password is a 401 — an ordinary refused request, not
      // a dead session, so it stays inside this dialog.
      toast.showError(error, "Nothing was downloaded");
    } finally {
      setExporting(false);
    }
  };

  if (failure) {
    return (
      <div className="nd-view nd-set">
        <EmptyState title={failure.title} body={failure.body} />
      </div>
    );
  }
  if (report === null) {
    return (
      <div className="nd-view nd-set">
        <div className="nd-empty">
          <Spinner large label="Loading settings" />
        </div>
      </div>
    );
  }

  return (
    <div className="nd-view nd-view--wide nd-set">
      <header className="nd-view__header">
        <div>
          <h1>Settings</h1>
          <p className="nd-meta nd-set__subtitle">
            Configuration is a ladder: default, then{" "}
            <code className="nd-mono">settings.env</code>, then the environment.
            The file is{" "}
            <code className="nd-mono">{report.path ?? "not bound"}</code>.
          </p>
        </div>
        {adoptCandidates.length > 0 ? (
          <button
            type="button"
            className="nd-button"
            onClick={() => setAdoptOpen(true)}
            disabled={adopting}
          >
            Adopt from environment ({adoptCandidates.length})
          </button>
        ) : null}
      </header>

      {report.unreadable ? (
        <p className="nd-set__warning" role="alert">
          {report.unreadable} — every row below falls back to the environment
          and the defaults until the file parses again.
        </p>
      ) : null}
      {report.unknown_keys.length > 0 ? (
        <p className="nd-set__warning" role="alert">
          This build does not configure{" "}
          {report.unknown_keys.join(", ")}, but they are kept in the file.
        </p>
      ) : null}
      {report.mixed_model_note ? (
        <div className="nd-set__warning" role="alert">
          <p>{report.mixed_model_note}</p>
          <button
            type="button"
            className="nd-button nd-button--primary nd-button--small"
            onClick={rebuildVectors}
            disabled={rebuilding}
          >
            {rebuilding ? "Rebuilding…" : "Rebuild vector index"}
          </button>
        </div>
      ) : null}

      {GROUPS.map((group) => (
        <section key={group.id} className="nd-set-section">
          <h2 className="nd-set-section__title">{group.title}</h2>
          {group.keys.map((key) => {
            const row = rowsByKey.get(key);
            if (row === undefined) return null;
            const blocker = editBlocker(row, capabilities);
            const draft = drafts.get(key);
            const dirty = draft !== undefined && draft !== row.value;
            const revert = revertTarget(row, eventsByKey.get(key) ?? null, capabilities);
            return (
              <div key={key} className="nd-set-row">
                <div className="nd-set-row__head">
                  <code className="nd-mono nd-set-row__name">{key}</code>
                  <span className={`nd-badge nd-badge--provenance-${row.provenance}`}>
                    {layerLabel(row.provenance)}
                  </span>
                  {blocker ? <span className="nd-meta">{blocker}</span> : null}
                </div>
                <div className="nd-set-row__body">
                  {blocker ? (
                    <input
                      name={key}
                      className="nd-input nd-input--mono"
                      type={row.secret ? "password" : "text"}
                      value={valueText(row)}
                      disabled
                      readOnly
                      aria-label={`${key} (read-only)`}
                    />
                  ) : (
                    <>
                      <input
                        name={key}
                        className="nd-input nd-input--mono"
                        type={row.secret ? "password" : "text"}
                        placeholder={row.secret ? (row.set ? "set" : "not set") : "not set"}
                        value={draft ?? (row.secret ? "" : (row.value ?? ""))}
                        onChange={(event) =>
                          setDraft(key, (event.target as HTMLInputElement).value)
                        }
                        aria-label={key}
                      />
                      <button
                        type="button"
                        className="nd-button nd-button--primary nd-button--small"
                        onClick={() => saveRow(row)}
                        disabled={!dirty || draft === "" || savingKey !== null}
                        title={
                          draft === ""
                            ? "An empty value is refused — remove the key to unset it"
                            : undefined
                        }
                      >
                        {savingKey === key ? "Saving…" : "Save"}
                      </button>
                    </>
                  )}
                  {revert?.kind === "write" ? (
                    <button
                      type="button"
                      className="nd-button nd-button--ghost nd-button--small"
                      onClick={() => revertRow(row)}
                      disabled={revertingKey !== null}
                      title={
                        revert.kind === "write" ? `Restore "${revert.value}"` : undefined
                      }
                    >
                      {revertingKey === key ? "Reverting…" : "Revert"}
                    </button>
                  ) : null}
                  {revert?.kind === "remove" ? (
                    <button
                      type="button"
                      className="nd-button nd-button--ghost nd-button--small"
                      onClick={() => revertRow(row)}
                      disabled={revertingKey !== null}
                      title="The previous state was: not stored"
                    >
                      {revertingKey === key ? "Removing…" : "Remove"}
                    </button>
                  ) : null}
                </div>
                <div className="nd-set-row__meta nd-meta">
                  Default: {row.secret ? "—" : (row.default ?? "none")}
                  {row.stored ? " · stored in settings.env" : ""}
                  {revert?.kind === "reenter" ? (
                    <span> · the previous secret value cannot be restored — re-enter it</span>
                  ) : null}
                  {key === "NODUM_LLM_CONTEXT_TOKENS" ? (
                    <span className="nd-set-row__note"> · {PROFILE_DEFAULT_NOTE}</span>
                  ) : null}
                </div>
                {key === "NODUM_EMBED_DOWNLOAD" ? (
                  <p className="nd-set-row__note">{EMBED_DOWNLOAD_NOTE}</p>
                ) : null}
                {notes.has(key) ? (
                  <p className="nd-set-row__liveness" role="status">
                    {notes.get(key)}
                  </p>
                ) : null}
              </div>
            );
          })}
          {group.id === "gardener" ? (
            <p className="nd-set-row__note nd-set__killswitch">
              Lowering these budgets does not stop a cycle already spending.
              To ask a running cycle to wind down, use the stop control on its
              entry in the <Link to="/journal">Journal</Link>.
            </p>
          ) : null}
        </section>
      ))}

      <section className="nd-set-section" aria-label="Export">
        <h2 className="nd-set-section__title">Export</h2>
        <p className="nd-meta nd-set__subtitle">
          Freeze the configuration as it runs — every value in force,
          environment-pinned ones included — as a{" "}
          <code className="nd-mono">.env</code> file docker compose reads back
          exactly.
        </p>
        <div className="nd-set-export-actions">
          <button
            type="button"
            className="nd-button"
            onClick={exportRedacted}
            disabled={exporting}
          >
            Download .env (redacted)
          </button>
          <button
            type="button"
            className="nd-button"
            onClick={() => setExportOpen(true)}
            disabled={exporting}
          >
            Download .env with API key…
          </button>
        </div>
      </section>

      {exportOpen ? (
        <Modal
          title="Download the export with your API key"
          onClose={() => {
            setExportOpen(false);
            setExportPassword("");
          }}
        >
          {WITH_KEYS_EXPORT_CONFIRM.map((line) => (
            <p key={line}>{line}</p>
          ))}
          <input
            name="password"
            type="password"
            className="nd-input"
            placeholder="Your account password"
            value={exportPassword}
            onChange={(event) => setExportPassword((event.target as HTMLInputElement).value)}
            aria-label="Your account password"
            autoComplete="current-password"
          />
          <button
            type="button"
            className="nd-button nd-button--primary"
            onClick={exportWithKeys}
            disabled={exporting || exportPassword === ""}
          >
            {exporting ? "Exporting…" : "Download with API key"}
          </button>
        </Modal>
      ) : null}

      {pendingModel !== null ? (
        <Modal
          title="Change the embedding model?"
          onClose={() => setPendingModel(null)}
        >
          {modelChangeConfirm(report.embed_chunks).map((line) => (
            <p key={line}>{line}</p>
          ))}
          <button
            type="button"
            className="nd-button nd-button--primary"
            onClick={() => void confirmModelChange()}
          >
            Change model
          </button>
          <button type="button" className="nd-button" onClick={() => setPendingModel(null)}>
            Cancel
          </button>
        </Modal>
      ) : null}

      {adoptOpen ? (
        <Modal title="Adopt settings from the environment" onClose={() => setAdoptOpen(false)}>
          <p>
            These names are pinned by the environment with a non-empty value.
            Adopting stores the same value in{" "}
            <code className="nd-mono">settings.env</code>, so unsetting the
            variable later no longer changes what is in force. The variable
            itself stays where it is — adopting never touches the host.
          </p>
          <ul className="nd-set-adopt-list">
            {adoptCandidates.map((candidate) => (
              <li key={candidate.key}>
                <code className="nd-mono">{candidate.key}</code>{" "}
                <span className="nd-meta">
                  = {candidate.value === null ? "(set)" : candidate.value}
                </span>
              </li>
            ))}
          </ul>
          <p className="nd-meta">
            A value this build refuses is skipped and named in the result, not
            a batch failure.
          </p>
          <button
            type="button"
            className="nd-button nd-button--primary"
            onClick={adopt}
            disabled={adopting}
          >
            {adopting ? "Adopting…" : `Adopt ${adoptCandidates.length}`}
          </button>
        </Modal>
      ) : null}
    </div>
  );
}

export { SettingsView };
