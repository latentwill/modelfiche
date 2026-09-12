import { Bot, ExternalLink, Terminal } from "lucide-react";
import { routeQuery } from "./api";
import { Page, Panel } from "./ui";

const REPOSITORY = "https://github.com/latentwill/modelfiche";

const operatorWorkflows = [
  { title: "Import and organize", body: "Connect an S3 source or choose a local folder, preview the detected content, then create a durable import.", href: "#/transfers", params: { mode: "import" }, label: "Open imports" },
  { title: "Build a dataset", body: "Create a draft, edit captions and inclusion state, then publish an immutable dataset version.", href: "#/datasets", params: {}, label: "Open datasets" },
  { title: "Generate and compare", body: "Select a project checkpoint, resolve the request plan, then queue Image, Eval, or Grid work.", href: "#/generate", params: {}, label: "Open generation" },
  { title: "Prepare training", body: "Select the owning workspace and dataset, configure signing, storage, and W&B ingress, then prepare a trainer packet.", href: "#/training-new", params: {}, label: "Open training" },
];

const references = [
  { title: "Windows app", body: "Portable download, launcher, data folders, CLI, updates, and troubleshooting.", href: `${REPOSITORY}/blob/main/docs/WINDOWS.md` },
  { title: "CLI guide", body: "Installation, discovery, output, exit codes, and operator workflows.", href: `${REPOSITORY}/blob/main/docs/CLI.md` },
  { title: "Agent guide", body: "JSON-first control flow, idempotency, safety rules, and copy-ready recipes.", href: `${REPOSITORY}/blob/main/docs/AGENT_GUIDE.md` },
  { title: "Training and W&B", body: "Trainer packets, signing, outbound checkpoint handoff, and optional live telemetry.", href: `${REPOSITORY}/blob/main/docs/WANDB_COMPATIBILITY.md` },
  { title: "Security boundary", body: "Local deployment assumptions, secret handling, private data, and reporting.", href: `${REPOSITORY}/blob/main/SECURITY.md` },
];

export function DocsScreen() {
  return <Page title="Docs" subtitle="Operator workflows, CLI conventions, and agent-safe automation from one place." actions={<a className="button vela-button" href={`${REPOSITORY}/tree/main/docs`} target="_blank" rel="noreferrer"><ExternalLink size={15} />GitHub docs</a>}>
    <Panel title="Start here" className="docs-intro">
      <p>Modelfiche is a local-first workbench. The browser and <code>mfiche</code> CLI use the same API, validation, durable jobs, activity history, and provider-admission rules.</p>
      <div className="actions"><a className="button vela-button vela-button-primary" href={`#/settings${routeQuery({})}`}>Open settings</a><a className="button vela-button" href={`#/readiness${routeQuery({})}`}>Run readiness checks</a></div>
    </Panel>

    <section aria-labelledby="operator-workflows-heading">
      <div className="vela-section-heading"><h2 id="operator-workflows-heading">Operator workflows</h2></div>
      <div className="docs-card-grid">
        {operatorWorkflows.map(workflow => <article className="docs-card" key={workflow.title}><h3>{workflow.title}</h3><p>{workflow.body}</p><a href={`${workflow.href}${routeQuery(workflow.params)}`}>{workflow.label}</a></article>)}
      </div>
    </section>

    <Panel title="Navigate and review">
      <p>Use the + menu to create or import within your current project. Search the workspace from the top bar on desktop or mobile; use the arrow keys to choose a result and Enter to open it.</p>
      <p>Gallery keeps search and project controls visible. Open More filters and display for image sets, dataset members, ratings, and thumbnail size. Filter and page changes are saved in the URL, so you can bookmark a view and return from image review to the same results.</p>
      <p>Press Escape to close a menu before closing its drawer or image review. Failed saves keep your input so you can retry. A saved form returns to its save action when you edit it again.</p>
    </Panel>

    <div className="docs-columns">
      <Panel title="Command line">
        <div className="docs-panel-heading"><Terminal size={20} /><p>Discover the installed command contract before automating a workflow.</p></div>
        <pre className="docs-command"><code>{`mfiche --json doctor
mfiche --json capabilities
mfiche --json enums
mfiche <group> --help`}</code></pre>
        <p>Put global options before the command group. Use <code>--json</code> for machine-readable stdout and <code>--quiet</code> to suppress wait progress.</p>
      </Panel>
      <Panel title="Agent contract">
        <div className="docs-panel-heading"><Bot size={20} /><p>Agents should use explicit context and verify durable terminal state.</p></div>
        <ol className="docs-steps"><li>Pass workspace and profile explicitly.</li><li>Review destructive work before submission.</li><li>Use one request ID per logical mutation.</li><li>Persist returned job and queue IDs.</li><li>Read the resulting resource before reporting success.</li></ol>
      </Panel>
      <Panel title="Verify training is connected">
        <p>Browser workspace selection does not select the CLI workspace. Pass <code>--workspace &lt;id-or-slug&gt;</code> on reads and writes; use the returned launch and run IDs.</p>
        <pre className="docs-command"><code>{`mfiche --json --workspace <workspace> training doctor
mfiche --json --workspace <workspace> training run preflight <launch>
mfiche --json --workspace <workspace> run metrics <run>
mfiche --json --workspace <workspace> run samples <run>
mfiche --json --workspace <workspace> run checkpoints <run>`}</code></pre>
        <p>Export the packet environment before starting the trainer: <code>set -a; . ./trainer.env; set +a</code>. Live losses and samples need reachable W&B ingress and the packet logger configuration. Explicit offline mode backs up files; it does not populate graphs or samples.</p>
      </Panel>
      <Panel title="Inspect an image before repairing">
        <pre className="docs-command"><code>{`mfiche --json --workspace <workspace> asset locations <asset>
mfiche --json --workspace <workspace> asset metadata <asset>
mfiche asset repair-images --help`}</code></pre>
        <p>A broken preview is not proof that the original is lost. Check durable locations, remote source availability, and the repair job result. Preserve asset IDs and lineage; do not edit the database or move storage files manually.</p>
      </Panel>
    </div>

    <section aria-labelledby="reference-heading">
      <div className="vela-section-heading"><h2 id="reference-heading">Reference</h2></div>
      <div className="docs-reference-list">
        {references.map(reference => <a href={reference.href} target="_blank" rel="noreferrer" key={reference.title}><span><strong>{reference.title}</strong><small>{reference.body}</small></span><ExternalLink size={15} aria-hidden="true" /></a>)}
      </div>
    </section>
  </Page>;
}
