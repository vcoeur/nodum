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
  GROUPS,
  PROFILE_DEFAULT_NOTE,
  adoptPreview,
  editBlocker,
  layerLabel,
  liveness,
  livenessLabel,
  revertTarget,
} from "./settingsModel";
import "./settings.css";

/** How many log events the revert column reads; settings events are rare. */
const EVENT_WINDOW = 200;

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

  const load = useCallback(async () => {
    const [settingsReport, eventPage] = await Promise.all([
      api.getSettings(),
      api.listEvents(EVENT_WINDOW),
    ]);
    setReport(settingsReport);
    setEvents(eventPage);
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [settingsReport, eventPage] = await Promise.all([
          api.getSettings(),
          api.listEvents(EVENT_WINDOW),
        ]);
        if (!cancelled) {
          setReport(settingsReport);
          setEvents(eventPage);
        }
      } catch (error) {
        if (!cancelled) setFailure(describeFailure(error, "the settings"));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

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

  const saveRow = (row: SettingOut) => {
    const draft = drafts.get(row.key);
    if (draft === undefined || savingKey !== null) return;
    setSavingKey(row.key);
    void api
      .applySettings({ [row.key]: draft })
      .then(() => {
        setSavingKey(null);
        clearDraft(row.key);
        setNote(row.key, livenessLabel(liveness(row.key)));
        return load();
      })
      .catch((error: unknown) => {
        setSavingKey(null);
        // The seam's own sentence is the message: unknown name, bad value,
        // or — for a stale client on a pinned row — SettingPinned's pin
        // sentence behind the 409.
        toast.showError(error, `${row.key} was not stored`);
      });
  };

  const revertRow = (row: SettingOut) => {
    const target = revertTarget(row, eventsByKey.get(row.key) ?? null, capabilities);
    if (target === null || revertingKey !== null) return;
    setRevertingKey(row.key);
    const write =
      target.kind === "write"
        ? api.applySettings({ [row.key]: target.value })
        : api.unsetSetting(row.key);
    void write
      .then(() => {
        setRevertingKey(null);
        setNote(row.key, `Reverted to the previous value${target.kind === "remove" ? " (removed)" : ""}`);
        return load();
      })
      .catch((error: unknown) => {
        setRevertingKey(null);
        toast.showError(error, `${row.key} was not reverted`);
      });
  };

  const adopt = () => {
    setAdopting(true);
    void api
      .adoptEnvironment()
      .then((result: SettingAdoptOut) => {
        setAdopting(false);
        setAdoptOpen(false);
        const skipped = result.skipped.map((skip) => skip.key).join(", ");
        toast.show(
          "success",
          `Adopted ${result.count} setting${result.count === 1 ? "" : "s"} from the environment`,
          skipped === "" ? undefined : `Refused values: ${skipped}`,
        );
        return load();
      })
      .catch((error: unknown) => {
        setAdopting(false);
        setAdoptOpen(false);
        toast.showError(error, "Nothing was adopted");
      });
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
                        disabled={!dirty || savingKey !== null}
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
