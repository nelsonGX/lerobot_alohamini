"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";
import { ProcConsole, ProcStatus, useProc } from "@/components/Console";
import { Button, Card, ErrorBox, Field, inputClass, StatusIcon, type Status } from "@/components/ui";
import { api, type JetsonStatus } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

export default function RobotPage() {
  const robot = usePoll(api.robot, 1500);
  const preflight = usePoll(api.preflight, 5000);
  const settings = usePoll(api.settings, 10000);
  const host = useProc("host");
  const teleop = useProc("teleop");
  const calibrate = useProc("calibrate");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const j = robot.data?.jetson;
  const recording = robot.data?.recording;
  const checks = preflight.data?.checks ?? [];
  const check = (id: string) => checks.find((c) => c.id === id);
  const leaderChecks = checks.filter((c) => c.id.startsWith("leader_"));
  const leadersOk = leaderChecks.length > 0 && leaderChecks.every((c) => c.status === "ok");

  const run = async (key: string, fn: () => Promise<unknown>, after: () => void = () => {}) => {
    setBusy(key);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
      after();
      robot.refresh();
      preflight.refresh();
    }
  };

  const hostRunning = host.proc?.state === "running" || host.proc?.state === "stopping";
  const hostState: { status: Status; label: string } = !j
    ? { status: "idle", label: "Checking…" }
    : j.host_listening
      ? { status: "ok", label: j.host_managed ? "Running" : "Running (started outside the panel)" }
      : hostRunning
        ? { status: "warn", label: host.proc?.state === "stopping" ? "Stopping…" : "Starting…" }
        : { status: "fail", label: "Not running" };
  const [cameras, setCameras] = useState<boolean | null>(null);
  const camerasOn = cameras ?? settings.data?.host_cameras ?? false;
  const calTarget = robot.data?.procs.calibrate_target;

  return (
    <div className="grid gap-6">
      <div>
        <h1 className="text-xl font-semibold">Robot</h1>
        <p className="mt-1 text-sm text-ink-2">
          Everything that used to need a terminal: start the robot host on the Jetson, try teleoperation, and calibrate arms.
        </p>
      </div>

      {/* Readiness overview */}
      <ol className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Step n={1} title="Jetson connected" status={!j ? "idle" : j.ssh_configured && j.reachable ? "ok" : "fail"}
          detail={!j ? "…" : !j.reachable ? `${j.ip} not reachable` : j.ssh_configured ? `${j.user}@${j.ip}` : "Set up access below"} />
        <Step n={2} title="Robot host" status={hostState.status} detail={hostState.label} />
        <Step n={3} title="Leader arms" status={!checks.length ? "idle" : leadersOk ? "ok" : "fail"}
          detail={!checks.length ? "…" : leadersOk ? "Both plugged in" : (leaderChecks.find((c) => c.status !== "ok")?.detail ?? "")} />
        <Step n={4} title="Leader calibration" status={(check("calibration")?.status as Status) ?? "idle"}
          detail={check("calibration")?.detail ?? "…"} />
      </ol>

      {error && <ErrorBox>{error}</ErrorBox>}
      {robot.error && <ErrorBox>Cannot reach the panel backend: {robot.error}</ErrorBox>}

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Jetson + host */}
        <div className="grid content-start gap-6">
          <JetsonCard j={j} onDone={() => { robot.refresh(); settings.refresh(); }} />

          <Card
            title={<span className="flex items-center gap-2"><StatusIcon status={hostState.status} /> Robot host (Jetson)</span>}
            actions={<ProcStatus proc={host.proc} />}
          >
            <p className="mb-3 text-sm text-ink-2">
              Drives the follower arms and sends their state to this machine. Must be running for teleoperation and recording.
            </p>
            <div className="flex flex-wrap items-center gap-3">
              {j?.host_listening || hostRunning ? (
                <Button
                  variant="danger"
                  disabled={busy === "host" || host.proc?.state === "stopping" || !!recording}
                  title={recording ? "Stop the recording first" : undefined}
                  onClick={() => run("host", api.hostStop, host.refresh)}
                >
                  Stop host
                </Button>
              ) : (
                <Button
                  variant="primary"
                  disabled={busy === "host" || !j?.ssh_configured || !j?.reachable}
                  onClick={() => run("host", () => api.hostStart(camerasOn), host.refresh)}
                >
                  {busy === "host" ? "Starting…" : "Start host"}
                </Button>
              )}
              <label className="flex items-center gap-2 text-sm text-ink-2">
                <input type="checkbox" checked={camerasOn} disabled={hostRunning} onChange={(e) => setCameras(e.target.checked)} />
                Record cameras
              </label>
              {recording && <span className="text-xs text-muted">Recording in progress: stop it before stopping the host.</span>}
            </div>
            {!camerasOn && !hostRunning && (
              <p className="mt-2 text-xs text-muted">Without cameras, datasets only contain joint data (like <code>./host</code>, which uses <code>--no_cameras</code>).</p>
            )}
            {host.proc && (
              <details className="mt-4" open={host.proc.state !== "exited" || !(host.proc.exit_code === 0 || host.proc.stopped)}>
                <summary className="cursor-pointer text-sm font-medium">Host output</summary>
                <div className="mt-2"><ProcConsole name="host" proc={host.proc} lines={host.lines} onChange={host.refresh} /></div>
              </details>
            )}
          </Card>
        </div>

        {/* Teleop + calibration */}
        <div className="grid content-start gap-6">
          <Card title="Teleoperate (no recording)" actions={<ProcStatus proc={teleop.proc} runningLabel="Teleoperating" />}>
            <p className="mb-3 text-sm text-ink-2">
              Practise a task or check the robot follows the leader arms. Nothing is saved. A follower arm stays limp until the
              matching leader arm is moved close to its pose.
            </p>
            <div className="flex flex-wrap gap-3">
              {teleop.proc?.state === "running" || teleop.proc?.state === "stopping" ? (
                <Button variant="danger" disabled={teleop.proc.state === "stopping"} onClick={() => run("teleop", () => api.procStop("teleop"), teleop.refresh)}>
                  Stop teleoperation
                </Button>
              ) : (
                <Button
                  variant="primary"
                  disabled={busy === "teleop" || !!recording || !j?.host_listening}
                  title={!j?.host_listening ? "Start the robot host first" : undefined}
                  onClick={() => run("teleop", api.teleopStart, teleop.refresh)}
                >
                  Start teleoperation
                </Button>
              )}
              {!j?.host_listening && <span className="self-center text-xs text-muted">Start the robot host first.</span>}
              {recording && <span className="self-center text-xs text-muted">A recording is running.</span>}
            </div>
            {teleop.proc && (
              <div className="mt-4"><ProcConsole name="teleop" proc={teleop.proc} lines={teleop.lines} onChange={teleop.refresh} /></div>
            )}
          </Card>

          <section id="calibration">
            <Card title="Calibration" actions={<ProcStatus proc={calibrate.proc} runningLabel={calTarget === "follower" ? "Calibrating follower" : "Calibrating leader"} />}>
              <p className="mb-3 text-sm text-ink-2">
                Needed once per arm, or after replacing a motor. The panel walks you through each step; just follow the blue box.
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                <CalButton
                  title="Leader arms (this machine)"
                  detail="Stops nothing on the robot. Teleoperation and recording must be stopped."
                  disabled={calibrate.proc?.state === "running" || !!recording || teleop.proc?.state === "running"}
                  onClick={() => run("cal", () => api.calibrateStart("leader"), calibrate.refresh)}
                />
                <CalButton
                  title="Follower arms (Jetson)"
                  detail="The robot host must be stopped first, because it uses the follower arms."
                  disabled={calibrate.proc?.state === "running" || !j?.ssh_configured || !!j?.host_listening || hostRunning}
                  onClick={() => run("cal", () => api.calibrateStart("follower"), calibrate.refresh)}
                />
              </div>
              {calibrate.proc && (
                <div className="mt-4 grid gap-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">{calibrate.proc.title}</span>
                    {calibrate.proc.state === "running" && (
                      <Button size="sm" variant="ghost" onClick={() => run("cal", () => api.procStop("calibrate"), calibrate.refresh)}>
                        Cancel
                      </Button>
                    )}
                  </div>
                  <ProcConsole name="calibrate" proc={calibrate.proc} lines={calibrate.lines} onChange={calibrate.refresh} />
                </div>
              )}
            </Card>
          </section>

          <p className="text-sm text-ink-2">
            Ready? <Link href="/" className="font-medium text-accent hover:underline">Go to Record →</Link>
          </p>
        </div>
      </div>
    </div>
  );
}

function Step({ n, title, status, detail }: { n: number; title: string; status: Status; detail: ReactNode }) {
  return (
    <li className="flex gap-3 rounded-xl border border-line bg-surface p-3">
      <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-surface-2 text-xs font-semibold text-ink-2">{n}</span>
      <div className="min-w-0">
        <div className="flex items-center gap-1.5 text-sm font-medium">
          <StatusIcon status={status} /> {title}
        </div>
        <div className="mt-0.5 truncate text-xs text-ink-2" title={typeof detail === "string" ? detail : undefined}>{detail}</div>
      </div>
    </li>
  );
}

function CalButton({ title, detail, disabled, onClick }: { title: string; detail: string; disabled: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="rounded-lg border border-line p-3 text-left transition hover:border-accent hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-line disabled:hover:bg-transparent"
    >
      <div className="text-sm font-medium">Calibrate {title}</div>
      <div className="mt-1 text-xs text-muted">{detail}</div>
    </button>
  );
}

function JetsonCard({ j, onDone }: { j: JetsonStatus | undefined; onDone: () => void }) {
  const [editing, setEditing] = useState(false);
  const [user, setUser] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const showForm = editing || (j && !j.ssh_configured);

  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.jetsonSetup(user.trim(), password);
      setPassword("");
      setEditing(false);
      setResult(r.repo ? `Connected. Found the robot code in ${r.repo}.` : "Connected, but could not find the lerobot_alohamini folder. Set it under Settings → Jetson repo folder.");
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title={<span className="flex items-center gap-2"><StatusIcon status={!j ? "idle" : j.ssh_configured && j.reachable ? "ok" : "fail"} /> Jetson</span>}
      actions={j?.ssh_configured && !editing && <Button size="sm" variant="ghost" onClick={() => { setEditing(true); setUser(j.user); }}>Reconnect</Button>}
    >
      {!j ? (
        <p className="text-sm text-muted">Checking…</p>
      ) : (
        <div className="grid gap-3 text-sm">
          {!j.reachable && (
            <ErrorBox>
              Cannot reach {j.ip}. Check the Jetson is powered on and on the network, and that the IP in Settings (gear icon) is right.
            </ErrorBox>
          )}
          {j.ssh_configured && !editing && (
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-ink-2">
              <dt className="text-muted">Login</dt><dd className="font-mono">{j.user}@{j.ip}</dd>
              <dt className="text-muted">Robot code</dt><dd className="break-all font-mono">{j.repo || "home folder (not found; set in Settings)"}</dd>
            </dl>
          )}
          {result && <p className="text-good-ink">{result}</p>}
          {showForm && (
            <form
              className="grid gap-3"
              onSubmit={(e) => {
                e.preventDefault();
                connect();
              }}
            >
              <p className="text-ink-2">
                One-time setup: log in to the Jetson once so the panel can start the robot host for everyone. The password is used
                once to install a key and is not stored.
              </p>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Jetson username">
                  <input className={inputClass} value={user} onChange={(e) => setUser(e.target.value)} autoComplete="off" required />
                </Field>
                <Field label="Password">
                  <input className={inputClass} type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="off" required />
                </Field>
              </div>
              {error && <ErrorBox>{error}</ErrorBox>}
              <div className="flex gap-2">
                <Button type="submit" variant="primary" disabled={busy || !user || !password || !j.reachable}>
                  {busy ? "Connecting…" : "Connect"}
                </Button>
                {editing && <Button type="button" onClick={() => setEditing(false)}>Cancel</Button>}
              </div>
            </form>
          )}
        </div>
      )}
    </Card>
  );
}
