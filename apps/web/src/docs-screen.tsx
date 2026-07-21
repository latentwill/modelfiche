import { Bot, ExternalLink, Terminal } from "lucide-react";
import { Page, Panel } from "./ui";

const REPOSITORY = "https://github.com/latentwill/modelfiche";

const operatorWorkflows = [
  { title: "Import and organize", body: "Connect a verified S3 source or choose a local folder, preview the detected content, then create a durable import.", href: "#/transfers?mode=import", label: "Open imports" },
  { title: "Build a dataset", body: "Create a draft, edit captions and inclusion state, then publish an immutable dataset version.", href: "#/datasets", label: "Open datasets" },
  { title: "Generate and compare", body: "Select a project checkpoint, review the request count and billing consequence, then queue Image, Eval, or Grid work.", href: "#/generate", label: "Open generation" },
  { title: "Prepare training", body: "Verify signing and outbound storage, create a launch, and generate a checksummed AI Toolkit trainer packet.", href: "#/training-new", label: "Open training" },
];

const references = [
  { title: "CLI guide", body: "Installation, discovery, output, exit codes, and operator workflows.", href: `${REPOSITORY}/blob/main/docs/CLI.md` },
  { title: "Agent guide", body: "JSON-first control flow, idempotency, safety rules, and copy-ready recipes.", href: `${REPOSITORY}/blob/main/docs/AGENT_GUIDE.md` },
  { title: "Training and W&B", body: "Trainer packets, signing, outbound checkpoint handoff, and optional live telemetry.", href: `${REPOSITORY}/blob/main/docs/WANDB_COMPATIBILITY.md` },
  { title: "Security boundary", body: "Local deployment assumptions, secret handling, private data, and reporting.", href: `${REPOSITORY}/blob/main/SECURITY.md` },
];

export function DocsScreen() {
  return <Page title="Docs" subtitle="Operator workflows, CLI conventions, and agent-safe automation from one place." actions={<a className="button vela-button" href={`${REPOSITORY}/tree/main/docs`} target="_blank" rel="noreferrer"><ExternalLink size={15} />GitHub docs</a>}>
    <Panel title="Start here" className="docs-intro">
      <p>Modelfiche is a local-first workbench. The browser and <code>mfiche</code> CLI use the same API, validation, durable jobs, activity history, and provider-admission rules.</p>
      <div className="actions"><a className="button vela-button vela-button-primary" href="#/settings">Open settings</a><a className="button vela-button" href="#/readiness">Run readiness checks</a></div>
    </Panel>

    <section aria-labelledby="operator-workflows-heading">
      <div className="vela-section-heading"><h2 id="operator-workflows-heading">Operator workflows</h2></div>
      <div className="docs-card-grid">
        {operatorWorkflows.map(workflow => <article className="docs-card" key={workflow.title}><h3>{workflow.title}</h3><p>{workflow.body}</p><a href={workflow.href}>{workflow.label}</a></article>)}
      </div>
    </section>

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
        <ol className="docs-steps"><li>Pass workspace and profile explicitly.</li><li>Preview billable or destructive work.</li><li>Use one request ID per logical mutation.</li><li>Persist returned job and queue IDs.</li><li>Read the resulting resource before reporting success.</li></ol>
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
