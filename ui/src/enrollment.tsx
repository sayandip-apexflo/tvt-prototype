import { useCallback, useEffect, useRef, useState } from "react";
import { get, send } from "./api";
import {
  ConfirmButton, Field, FormActions, Icon, Modal, Panel, StatusPill, submitForm,
} from "./components";
import type { EnrollmentStatusResponse, PendingPerson } from "./types";

type Mutate = (promise: Promise<unknown>, success: string) => Promise<boolean>;

async function safeGet<T>(path: string, fallback: T): Promise<T> {
  try { return await get<T>(path); } catch { return fallback; }
}

const IN_PROGRESS = new Set(["activating", "capturing", "restoring"]);

const STATUS_COPY: Record<string, string> = {
  activating: "Switching camera to enrollment mode…",
  capturing: "Ready — capture the person's face now",
  restoring: "Restoring normal operation…",
  completed: "Normal operation restored",
  timed_out: "No capture arrived in time — camera restored",
  cancelled: "Cancelled — camera restored",
  failed: "Could not switch camera — nothing changed",
};

function pillValue(status: string): string {
  if (status === "completed") return "healthy";
  if (status === "timed_out" || status === "failed") return "warning";
  if (status === "cancelled") return "unconfigured";
  return "degraded";
}

function fmtCountdown(deadline: string | null): string | null {
  if (!deadline) return null;
  const seconds = Math.round((new Date(deadline).getTime() - Date.now()) / 1000);
  if (seconds <= 0) return "any moment";
  if (seconds < 60) return `${seconds}s`;
  return `${Math.ceil(seconds / 60)}m`;
}

export function NamePersonModal(
  { person, close, mutate }: { person: PendingPerson; close: () => void; mutate: Mutate },
) {
  return <Modal
    title="Name this person"
    description="Stored only in the identity system (never in this browser or the camera-inventory database)."
    onClose={close}
  >
    <form className="form-grid" onSubmit={(event) => submitForm(event, async (form) => {
      const displayName = String(form.get("display_name") || "").trim();
      if (
        await mutate(
          send(`/api/v1/enrollment/people/${encodeURIComponent(person.person_id)}/name`, "POST", {
            display_name: displayName,
          }),
          "Person named",
        )
      ) close();
    })}>
      <Field label="Display name" wide hint="Shown in attendance and identity reports">
        <input name="display_name" required maxLength={160} autoFocus />
      </Field>
      <FormActions>
        <button type="button" className="button secondary" onClick={close}>Name later</button>
        <button className="button" type="submit">Save name</button>
      </FormActions>
    </form>
  </Modal>;
}

/** Persistent surface for naming people captured during enrollment -- stays
 * reachable even if the capture-accepted modal was closed or the browser
 * that started the session is gone (server-side restoration already
 * happened independently; naming is a separate, un-timed step). */
export function PendingPeoplePanel({ mutate }: { mutate: Mutate }) {
  const [people, setPeople] = useState<PendingPerson[] | null>(null);
  const [naming, setNaming] = useState<PendingPerson | null>(null);
  const load = useCallback(async () => {
    setPeople(await safeGet<PendingPerson[]>("/api/v1/enrollment/people", []));
  }, []);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load]);
  const combinedMutate: Mutate = async (promise, message) => {
    const succeeded = await mutate(promise, message);
    if (succeeded) await load();
    return succeeded;
  };
  if (!people || !people.length) return null;
  return <Panel
    title="People awaiting a name"
    subtitle="Captured during enrollment. Their camera has already been restored to normal operation -- naming never blocks that."
  >
    <div className="pending-people-list">
      {people.map((person) => <div className="line-item" key={person.person_id}>
        <span className="camera-glyph"><Icon name="eye" /></span>
        <span><strong>Unnamed person</strong><small>{person.camera_id || "—"} · {person.deployment_key}</small></span>
        <button className="button small" onClick={() => setNaming(person)}>Name</button>
      </div>)}
    </div>
    {naming && <NamePersonModal person={naming} close={() => setNaming(null)} mutate={combinedMutate} />}
  </Panel>;
}

function StartEnrollmentModal(
  { deploymentId, close, mutate }: { deploymentId: string; close: () => void; mutate: Mutate },
) {
  return <Modal
    title="Start enrollment"
    description="The designated camera will exclusively run face enrollment until a capture is accepted, it is cancelled, or it times out. Its normal face recognition / ANPR analytics pause for the duration -- restoration happens automatically and does not require this page to stay open."
    onClose={close}
  >
    <form className="form-grid" onSubmit={(event) => submitForm(event, async (form) => {
      const minutes = form.get("capture_window_minutes");
      const capture_window_seconds = minutes ? Number(minutes) * 60 : undefined;
      if (
        await mutate(
          send(`/api/v1/deployments/${encodeURIComponent(deploymentId)}/enrollment/sessions`, "POST", {
            capture_window_seconds,
          }),
          "Switching camera to enrollment mode",
        )
      ) close();
    })}>
      <Field label="Capture window (minutes)" wide hint="Defaults to 5 minutes; the camera restores automatically if nobody is captured in time">
        <input name="capture_window_minutes" type="number" min="1" max="30" placeholder="5" />
      </Field>
      <FormActions>
        <button type="button" className="button secondary" onClick={close}>Cancel</button>
        <button className="button" type="submit">Start enrollment</button>
      </FormActions>
    </form>
  </Modal>;
}

function DesignateModal(
  { deploymentId, cameraId, close, mutate }: { deploymentId: string; cameraId: string; close: () => void; mutate: Mutate },
) {
  return <Modal
    title="Designate enrollment camera"
    description="Any face-recognition camera on this deployment can be temporarily borrowed for enrollment. Only one camera can be designated per deployment, and it cannot be changed while a session is active."
    onClose={close}
  >
    <p>Use <strong>{cameraId}</strong> as this deployment's enrollment camera?</p>
    <FormActions>
      <button type="button" className="button secondary" onClick={close}>Cancel</button>
      <button
        className="button"
        onClick={() => void (async () => {
          if (
            await mutate(
              send(`/api/v1/deployments/${encodeURIComponent(deploymentId)}/enrollment/camera`, "POST", {
                camera_id: cameraId,
              }),
              "Enrollment camera designated",
            )
          ) close();
        })()}
      >
        Designate
      </button>
    </FormActions>
  </Modal>;
}

/** Rendered per camera-assignment row in the camera drawer (see
 * ui/src/App.tsx's CameraDetail). Enrollment is a deployment-scoped mode,
 * not a per-camera application choice -- see docs/contracts/tvt-mills-v1/
 * README.md -- so this reads/writes the deployment's single designation and
 * session rather than anything on the camera's own app list. */
export function EnrollmentAssignmentControl(
  { cameraId, deploymentId, apps, mutate }: { cameraId: string; deploymentId: string; apps: string[]; mutate: Mutate },
) {
  const [status, setStatus] = useState<EnrollmentStatusResponse | null>(null);
  const [showDesignate, setShowDesignate] = useState(false);
  const [showStart, setShowStart] = useState(false);
  const [namingPrompt, setNamingPrompt] = useState<PendingPerson | null>(null);
  const acknowledgedSessionId = useRef<string | null>(null);

  const load = useCallback(async () => {
    setStatus(await safeGet<EnrollmentStatusResponse | null>(
      `/api/v1/deployments/${encodeURIComponent(deploymentId)}/enrollment/status`, null,
    ));
  }, [deploymentId]);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 2000);
    return () => window.clearInterval(timer);
  }, [load]);

  const combinedMutate: Mutate = async (promise, message) => {
    const succeeded = await mutate(promise, message);
    if (succeeded) await load();
    return succeeded;
  };

  useEffect(() => {
    const session = status?.session;
    if (
      session
      && session.camera_id === cameraId
      && session.capture_result === "created"
      && session.naming_status === "pending_name"
      && acknowledgedSessionId.current !== session.session_id
    ) {
      acknowledgedSessionId.current = session.session_id;
      setNamingPrompt({
        session_id: session.session_id, person_id: session.person_id as string,
        deployment_key: session.deployment_key, camera_id: session.camera_id,
        captured_at: session.captured_at,
      });
    }
  }, [status, cameraId]);

  if (!status) return null;
  const isDesignated = status.designated_camera_id === cameraId;
  const session = isDesignated ? status.session : null;

  let body: JSX.Element | null = null;

  if (session && IN_PROGRESS.has(session.status)) {
    const countdown = session.status === "capturing" ? fmtCountdown(session.capture_deadline_at) : null;
    body = <div className="enrollment-progress">
      <div className="button-row">
        <StatusPill value={pillValue(session.status)} label={STATUS_COPY[session.status]} />
        {status.degraded && <StatusPill value="unavailable" label="K3s unreachable -- retrying" />}
      </div>
      {countdown && <small className="table-sub">Capture window closes in {countdown}</small>}
      {session.status !== "restoring" && (
        <ConfirmButton
          className="button small secondary"
          message="Cancel enrollment and restore this camera's normal apps now?"
          onConfirm={() => void combinedMutate(
            send(
              `/api/v1/deployments/${encodeURIComponent(deploymentId)}/enrollment/sessions/${encodeURIComponent(session.session_id)}/cancel`,
              "POST",
            ),
            "Cancelling — restoring normal operation",
          )}
        >
          Cancel
        </ConfirmButton>
      )}
    </div>;
  } else if (session && (session.status === "completed" || session.status === "timed_out" || session.status === "cancelled" || session.status === "failed")) {
    body = <div className="enrollment-progress">
      <div className="button-row">
        <StatusPill value={pillValue(session.status)} label={STATUS_COPY[session.status]} />
        <button className="button small secondary" onClick={() => setShowStart(true)}>Start enrollment</button>
      </div>
      {showStart && <StartEnrollmentModal deploymentId={deploymentId} close={() => setShowStart(false)} mutate={combinedMutate} />}
    </div>;
  } else if (isDesignated) {
    body = <div className="button-row">
      <StatusPill value="unconfigured" label="Designated enrollment camera" />
      <button className="button small secondary" onClick={() => setShowStart(true)}>Start enrollment</button>
      {showStart && <StartEnrollmentModal deploymentId={deploymentId} close={() => setShowStart(false)} mutate={combinedMutate} />}
    </div>;
  } else if (apps.includes("face_recognition")) {
    body = <>
      <button className="button small secondary" onClick={() => setShowDesignate(true)}>Designate as enrollment camera</button>
      {showDesignate && (
        <DesignateModal deploymentId={deploymentId} cameraId={cameraId} close={() => setShowDesignate(false)} mutate={combinedMutate} />
      )}
    </>;
  }

  return <>
    {body}
    {namingPrompt && <NamePersonModal person={namingPrompt} close={() => setNamingPrompt(null)} mutate={combinedMutate} />}
  </>;
}
