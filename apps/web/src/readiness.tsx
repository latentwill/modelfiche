import { useState } from "react";
import { CheckCircle2, Clipboard, PlugZap, RefreshCw, TriangleAlert } from "lucide-react";
import { api, idOf, jsonBody, listOf, str } from "./api";
import { useResource } from "./hooks";
import { Notice, Page, Panel, Status } from "./ui";

type Row = Record<string, unknown>;
const rows = (value: unknown) => listOf<Row>(value);

type ReadinessCard = {
  id: string;
  title: string;
  ready: boolean;
  evidence: string;
  next: string;
  href?: string;
};

export function ReadinessScreen() {
  const doctor = useResource<Row>("/api/system/doctor");
  const settings = useResource<Row>("/api/operator-settings");
  const sources = useResource<unknown>("/api/import-sources");
  const training = useResource<Row>("/api/training-setup");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const sourceRows = rows(sources.data);
  const activeSource = sourceRows.find(source => source.is_active !== false);
  const credentials = (settings.data?.credentials ?? {}) as Row;
  const backup = (doctor.data?.backup ?? {}) as Row;
  const agentCli = (doctor.data?.agent_cli ?? {}) as Row;
  const localReady = doctor.data?.database && (doctor.data.database as Row).ok === true && doctor.data?.asset_store && (doctor.data.asset_store as Row).ok === true && backup.configured === true;
  const storageReady = Boolean(activeSource);
  const generationReady = credentials.fal_configured === true;
  const trainingReady = training.data?.ready === true;
  const cards: ReadinessCard[] = [
    { id: "local", title: "Local data and backup", ready: Boolean(localReady), evidence: localReady ? `Database, asset store, and backup are ready. Latest: ${str(backup.latest)}` : "Local data is available, but no verified user backup is recorded.", next: "Run mfiche backup create, then check again." },
    { id: "storage", title: "S3 storage", ready: storageReady, evidence: storageReady ? `${str(activeSource?.name, "S3 source")} is configured.` : "No S3 source is connected.", next: activeSource ? "Test the selected source connection to check access." : "Open Storage settings and connect an S3 source.", href: "#/settings?section=storage" },
    { id: "generation", title: "Generation provider", ready: generationReady, evidence: generationReady ? "A write-only FAL credential is configured." : "No FAL credential is configured.", next: generationReady ? "Validate the credential without submitting generation." : "Open Generation settings and save a FAL key.", href: "#/settings?section=generation" },
    { id: "training", title: "Training", ready: trainingReady, evidence: trainingReady ? "Signing identity and outbound training storage passed; no tunnel is required." : "One or more training transport checks are blocked.", next: str((rows(training.data?.checks).find(check => check.status !== "pass") as Row | undefined)?.fix, "Open Training setup and resolve each blocked check."), href: "#/training-new" },
    { id: "agent", title: "Agent CLI", ready: agentCli.configured === true, evidence: agentCli.configured ? `mfiche is available at ${str(agentCli.path)}.` : agentCli.install_available ? `The packaged command can be installed at ${str(agentCli.install_path)}.` : "The packaged command is unavailable in this development runtime.", next: agentCli.configured ? "Run mfiche --help from Terminal." : agentCli.install_available ? "Install the command for this user. No administrator access is required." : "Use uv run mfiche from this checkout.", href: "#/settings?section=general" },
  ];
  const readyCount = cards.filter(card => card.ready).length;

  async function check(card: ReadinessCard) {
    setBusy(card.id); setMessage(""); setError("");
    try {
      if (card.id === "storage" && activeSource) await api(`/api/import-sources/${idOf(activeSource)}/test`, jsonBody({}));
      else if (card.id === "generation" && generationReady) await api("/api/operator-settings/test/fal", jsonBody({}));
      else if (card.id === "agent") {
        if (agentCli.configured && agentCli.path) await navigator.clipboard.writeText(`${str(agentCli.path)} --help`);
        else await api("/api/system/cli-install", jsonBody({}));
      }
      await Promise.all([doctor.reload(), settings.reload(), sources.reload(), training.reload()]);
      setMessage(card.id === "agent" ? agentCli.configured ? "CLI command copied." : "mfiche installed for this user." : `${card.title} checked.`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(""); }
  }

  return <Page title="Readiness" subtitle="Five checks for a complete local operator and agent workflow." actions={<Status value={readyCount === cards.length ? "ready" : `${readyCount} of ${cards.length} ready`} />}>
    <Notice error={error || doctor.error || settings.error || sources.error || training.error} loading={doctor.loading || settings.loading || sources.loading || training.loading} />
    {message && <div className="vela-notice" role="status"><strong>Readiness</strong><span>{message}</span></div>}
    <div className="readiness-card-grid">
      {cards.map(card => <Panel key={card.id} title={card.title}>
        <div className={`readiness-card-state ${card.ready ? "is-ready" : "is-blocked"}`}>{card.ready ? <CheckCircle2 size={22} /> : <TriangleAlert size={22} />}<Status value={card.ready ? "ready" : "action required"} /></div>
        <p>{card.evidence}</p>
        <div className="readiness-next"><strong>Next action</strong><span>{card.next}</span></div>
        <div className="actions">
          <button type="button" onClick={() => void check(card)} disabled={Boolean(busy) || (card.id === "storage" && !activeSource) || (card.id === "generation" && !generationReady) || (card.id === "agent" && !agentCli.configured && !agentCli.install_available)}>{card.id === "agent" ? <Clipboard size={14} /> : card.id === "storage" || card.id === "generation" ? <PlugZap size={14} /> : <RefreshCw size={14} />}{busy === card.id ? "Checking…" : card.id === "agent" ? agentCli.configured ? "Copy command" : "Install command" : "Check now"}</button>
          {card.href && <a className="button vela-button" href={card.href}>Open setup</a>}
        </div>
      </Panel>)}
    </div>
  </Page>;
}
