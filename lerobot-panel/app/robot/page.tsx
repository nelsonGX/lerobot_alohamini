"use client";

import { useState, type ReactNode } from "react";
import { HostMonitor } from "@/components/HostMonitor";
import { ProcConsole, ProcStatus, useProc } from "@/components/Console";
import { Button, Card, ErrorBox, Field, inputClass, LinkButton, Spinner, StatusIcon, Toggle, type Status } from "@/components/ui";
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
        <Step n={1} title="Jetson connected" status={!j ? "idle" : j.agent_ok ? "ok" : "fail"}
          detail={!j ? "…" : !j.reachable ? `${j.ip} not reachable` : j.agent_ok ? `${j.system?.hostname ?? j.ip} (agent running)` : !j.agent_running ? "Start ./agent on the Jetson" : "Enter the agent token below"} />
        <Step n={2} title="Robot host" status={hostState.status} detail={hostState.label} />
        <Step n={3} title="Leader arms" status={!checks.length ? "idle" : leadersOk ? "ok" : "fail"}
          detail={!checks.length ? "…" : leadersOk ? "Both plugged in" : (leaderChecks.find((c) => c.status !== "ok")?.detail ?? "")} />
        <Step n={4} title="Leader calibration" status={(check("calibration")?.status as Status) ?? "idle"}
          detail={check("calibration")?.detail ?? "…"} />
      </ol>

      {error && <ErrorBox>{error}</ErrorBox>}
      {robot.error && <ErrorBox>Cannot reach the panel backend: {robot.error}</ErrorBox>}

      {(j?.host_listening || hostRunning) && <HostMonitor />}

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Jetson + host */}
        <div className="grid content-start gap-6">
          <JetsonCard j={j} onDone={() => { robot.refresh(); settings.refresh(); }} />
          {j && <JetsonMonitor j={j} />}

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
                  disabled={!!recording}
                  loading={busy === "host" || host.proc?.state === "stopping"}
                  loadingText="Stopping…"
                  title={recording ? "Stop the recording first" : undefined}
                  onClick={() => run("host", api.hostStop, host.refresh)}
                >
                  Stop host
                </Button>
              ) : (
                <Button
                  variant="primary"
                  disabled={!j?.agent_ok}
                  loading={busy === "host"}
                  loadingText="Starting…"
                  title={!j?.agent_ok ? "Connect to the Jetson agent first" : undefined}
                  onClick={() => run("host", () => api.hostStart(camerasOn), host.refresh)}
                >
                  Start host
                </Button>
              )}
              <Toggle
                checked={camerasOn}
                disabled={hostRunning}
                onChange={setCameras}
                label="Record cameras"
                title={hostRunning ? "Stop the host to change this" : undefined}
              />
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
                <Button
                  variant="danger"
                  loading={busy === "teleop" || teleop.proc.state === "stopping"}
                  loadingText="Stopping…"
                  onClick={() => run("teleop", () => api.procStop("teleop"), teleop.refresh)}
                >
                  Stop teleoperation
                </Button>
              ) : (
                <Button
                  variant="primary"
                  disabled={!!recording || !j?.host_listening}
                  loading={busy === "teleop"}
                  loadingText="Starting…"
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
                  loading={busy === "cal" && calTarget !== "follower"}
                  active={calibrate.proc?.state === "running" && calTarget !== "follower"}
                  blockedReason={calibrate.proc?.state === "running" ? "A calibration is already running" : recording ? "A recording is running" : teleop.proc?.state === "running" ? "Stop teleoperation first" : null}
                  onClick={() => run("cal", () => api.calibrateStart("leader"), calibrate.refresh)}
                />
                <CalButton
                  title="Follower arms (Jetson)"
                  detail="The robot host must be stopped first, because it uses the follower arms."
                  loading={busy === "cal" && calTarget === "follower"}
                  active={calibrate.proc?.state === "running" && calTarget === "follower"}
                  blockedReason={calibrate.proc?.state === "running" ? "A calibration is already running" : !j?.agent_ok ? "Connect to the Jetson first" : j?.host_listening || hostRunning ? "Stop the robot host first" : null}
                  onClick={() => run("cal", () => api.calibrateStart("follower"), calibrate.refresh)}
                />
              </div>
              {calibrate.proc && (
                <div className="mt-4 grid gap-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">{calibrate.proc.title}</span>
                    {calibrate.proc.state === "running" && (
                      <Button size="sm" variant="ghost" className="text-critical-ink" onClick={() => run("cal", () => api.procStop("calibrate"), calibrate.refresh)}>
                        Cancel calibration
                      </Button>
                    )}
                  </div>
                  <ProcConsole name="calibrate" proc={calibrate.proc} lines={calibrate.lines} onChange={calibrate.refresh} />
                </div>
              )}
            </Card>
          </section>

          <div className="flex items-center justify-between gap-3 rounded-lg border border-line bg-surface px-4 py-3 text-sm text-ink-2">
            Robot ready? Head over and start a session.
            <LinkButton href="/" variant="primary" size="sm">Go to Record →</LinkButton>
          </div>
        </div>
      </div>
    </div>
  );
}

function Step({ n, title, status, detail }: { n: number; title: string; status: Status; detail: ReactNode }) {
  return (
    <li className="flex gap-3 rounded-lg border border-line bg-surface p-3">
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

function CalButton({ title, detail, blockedReason, loading, active, onClick }: {
  title: string;
  detail: string;
  blockedReason: string | null;
  loading: boolean;
  active: boolean;
  onClick: () => void;
}) {
  const disabled = !!blockedReason || loading;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={blockedReason ?? undefined}
      className={`group flex flex-col rounded-lg border p-3 text-left transition enabled:hover:-translate-y-px enabled:hover:border-accent enabled:hover:shadow-md enabled:active:translate-y-0 disabled:cursor-not-allowed ${active ? "border-accent bg-accent/5" : "border-line"} ${disabled && !active ? "opacity-50" : ""}`}
    >
      <span className="flex items-center justify-between gap-2 text-sm font-medium">
        Calibrate {title}
        {loading ? (
          <Spinner />
        ) : (
          <span aria-hidden className="text-muted transition group-enabled:group-hover:translate-x-0.5 group-enabled:group-hover:text-accent">→</span>
        )}
      </span>
      <span className="mt-1 text-xs text-muted">{detail}</span>
      {blockedReason && !active && <span className="mt-2 text-xs font-medium text-warn-ink">{blockedReason}</span>}
      {active && <span className="mt-2 text-xs font-medium text-accent">In progress — follow the steps below</span>}
    </button>
  );
}

function JetsonCard({ j, onDone }: { j: JetsonStatus | undefined; onDone: () => void }) {
  const [editing, setEditing] = useState(false);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const showForm = editing || (j && j.reachable && j.agent_running && !j.agent_ok);

  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.jetsonSetup(token.trim());
      setToken("");
      setEditing(false);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title={<span className="flex items-center gap-2"><StatusIcon status={!j ? "idle" : j.agent_ok ? "ok" : "fail"} /> Jetson</span>}
      actions={j?.agent_ok && !editing && <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>Change token</Button>}
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
          {j.reachable && !j.agent_running && (
            <ErrorBox>
              The panel agent is not running on the Jetson. Open a terminal there once and run <code className="font-mono">./agent</code> in the
              repo (or install it as a service, see the panel README). It prints a token to paste here.
            </ErrorBox>
          )}
          {j.token_rejected && !editing && <ErrorBox>The agent rejected the saved token. Paste the current one below.</ErrorBox>}
          {j.agent_ok && !editing && j.system && (
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-ink-2">
              <dt className="text-muted">Machine</dt><dd className="font-mono">{j.system.hostname} · {j.ip}</dd>
              <dt className="text-muted">Robot code</dt><dd className="break-all font-mono">{j.system.repo}</dd>
            </dl>
          )}
          {showForm && (
            <form className="grid gap-3" onSubmit={(e) => { e.preventDefault(); connect(); }}>
              <p className="text-ink-2">
                Paste the token that <code className="font-mono">./agent</code> printed on the Jetson (also in <code className="font-mono">lerobot-panel/.data/agent_token</code> there).
              </p>
              <Field label="Agent token">
                <input className={`${inputClass} font-mono`} value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" spellCheck={false} autoFocus required />
              </Field>
              {error && <ErrorBox>{error}</ErrorBox>}
              <div className="flex gap-2">
                <Button type="submit" variant="primary" disabled={!token.trim() || !j.agent_running} loading={busy} loadingText="Connecting…">
                  Connect
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

const fmtGB = (b: number) => `${(b / 1e9).toFixed(b > 1e11 ? 0 : 1)} GB`;
const fmtUp = (s: number) => (s >= 86400 ? `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h` : s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : `${Math.floor(s / 60)}m`);

function Meter({ label, pct, detail }: { label: string; pct: number; detail: string }) {
  const tone = pct > 90 ? "bg-critical" : pct > 75 ? "bg-warn" : "bg-accent";
  return (
    <div>
      <div className="flex justify-between text-xs"><span className="text-muted">{label}</span><span className="font-mono text-ink-2">{detail}</span></div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-2"><div className={`h-full ${tone}`} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} /></div>
    </div>
  );
}

function JetsonMonitor({ j }: { j: JetsonStatus }) {
  const s = j.system;
  if (!j.agent_ok || !s) return null;
  const hot = Math.max(0, ...s.temps.map((t) => t.c));
  return (
    <Card title="Jetson monitor" actions={<span className="text-xs text-muted">up {fmtUp(s.uptime_s)} · live</span>}>
      <div className="grid gap-4 text-sm sm:grid-cols-2">
        <div className="grid gap-3">
          <Meter label="CPU" pct={s.cpu_percent ?? 0} detail={`${s.cpu_percent ?? "?"}% · ${s.cpus} cores · load ${s.load[0].toFixed(2)}`} />
          <Meter label="Memory" pct={(100 * s.mem.used) / s.mem.total} detail={`${fmtGB(s.mem.used)} / ${fmtGB(s.mem.total)}`} />
          <Meter label="Disk" pct={(100 * s.disk.used) / s.disk.total} detail={`${fmtGB(s.disk.used)} / ${fmtGB(s.disk.total)}`} />
          {s.temps.length > 0 && <Meter label="Hottest sensor" pct={hot} detail={`${hot.toFixed(0)} °C`} />}
        </div>
        <div>
          <div className="mb-1 text-xs text-muted">Devices</div>
          {s.devices.length === 0 ? (
            <p className="text-ink-2">No robot devices found (no <code className="font-mono">/dev/am_*</code> links). Are the arm adapters plugged in?</p>
          ) : (
            <ul className="grid gap-1 font-mono text-xs">
              {s.devices.map((d) => (
                <li key={d.name} className="flex items-center gap-2"><StatusIcon status={d.ok ? "ok" : "fail"} /> {d.name}</li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Card>
  );
}
