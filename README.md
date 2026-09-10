<div align="center">

# ModelFiche

### A visual workbench for your image models.

**Organize datasets. Compare training results. Keep track of what worked.**

[Get started](#get-started) · [Take a look](#a-look-inside) · [Documentation](docs/README.md) · [CLI & agents](docs/AGENT_GUIDE.md)

</div>

![ModelFiche Gallery in its white/light theme, showing Airbrush experiment outputs in the Embeddings workspace.](docs/images/airbrush-gallery-light.png)

ModelFiche keeps your **datasets, captions, training runs, checkpoints, and generated images together** in a local-first app. It's for people training and evaluating image models and LoRAs who want to see what changed, compare the results, and find the model worth keeping.

## 🎯 Why ModelFiche?

A good sample is only useful if you can trace it back to the model that made it.
Once you have several dataset versions, training runs, and folders of `.safetensors`
files, that gets harder than it should be.

ModelFiche gives that work a visual home:

| When you need to… | ModelFiche helps you… |
| --- | --- |
| Remember what went into a run | Keep dataset versions, captions, and training artifacts connected. |
| Choose a checkpoint | Inspect training samples by step and compare evaluation results. |
| Test what makes a difference | Build grids across prompts, steps, seeds, strengths, or models. |
| Keep your decisions | Rate images, record review notes, and promote selected model versions. |
| Pick up an older experiment | Find its images, model files, metadata, and history in the same project. |

**Your trainer does the training. ModelFiche organizes the work around it.**
Use it alongside AI Toolkit, import existing local or S3-compatible archives,
and optionally connect live training metrics and samples.

<a id="a-look-inside"></a>

## 🖼️ A look inside

### Your project, in one place

Move from source images to training samples, model files, and saved grids without
losing the project context. The inspector keeps metadata, notes, and activity close
at hand.

*The opening screenshot shows Airbrush experiment outputs in the Embeddings
workspace, using the white/light theme.*

### Datasets you can come back to

Inspect images and captions, make batch edits, and publish dataset versions so
you can keep track of the material used for each experiment.

### Compare, review, and keep the result

Browse source images and generated outputs in the Gallery. Compare training
samples by step, evaluate models against prompt sets, and inspect saved grids
at full size. Keep ratings and review notes alongside the work, then promote
the model versions you want to use.

<details>
<summary><strong>🌙 Prefer dark mode? See the same gallery in blue.</strong></summary>

![Airbrush experiment outputs in ModelFiche's blue dark theme.](docs/images/airbrush-gallery-dark.png)

</details>

### Built for hands-on work and automation

The browser UI and `mfiche` CLI share the same local service. Use the UI for visual
review and the JSON-first CLI for imports, inspection, trainer preparation,
exports, backups, and agent-driven workflows.

```text
Dataset → Training run → Checkpoint → Evaluation → Reviewed model version
```

<a id="get-started"></a>

## 🚀 Get started

### Windows app

Download **Modelfiche-0.1.0-windows-x64.zip** from
[Releases](https://github.com/latentwill/modelfiche/releases/latest), choose
**Extract All**, then open **Modelfiche.exe**. The app opens in your browser;
keep its launcher window open while you work. No Python or Node.js installation is needed. Supports Windows 10/11 on Intel/AMD 64-bit PCs.

[Windows setup, CLI, and troubleshooting](docs/WINDOWS.md)

### macOS app

Open the project's [Releases](https://github.com/latentwill/modelfiche/releases)
page for available builds. For a packaged build, download the DMG, drag
**ModelFiche** into Applications, and open it. The bundle includes the local
API, worker, web UI, and CLI.

**Current status:** active development, designed for a single operator. macOS
artifacts are unsigned. See [Security](SECURITY.md) for the supported deployment
boundary and remote-access limitations.

### Run from source

Requires **Python 3.11+, uv, Node.js, and pnpm**. See
[Contributing](CONTRIBUTING.md) for development details.

```bash
git clone https://github.com/latentwill/modelfiche.git
cd modelfiche
uv sync --extra dev --extra compat-wandb
pnpm --dir apps/web install
uv run alembic upgrade head
uv run mfiche start
```

Open **http://127.0.0.1:5173**. Use `uv run mfiche status` to check the stack
and `uv run mfiche stop` to stop it.

### Your first project

1. Open **Settings** and configure your local profile.
2. Use the **+** menu to create a project or import existing work.
3. Inspect your dataset, captions, model files, and training samples.
4. Compare results and keep notes on what you want to try next.

Connect S3-compatible storage when you need remote assets or checkpoint handoff.
Generation and evaluations through FAL require a configured credential and use
paid provider services. Live training graphs and samples require the
[W&B-compatible logging setup](docs/WANDB_COMPATIBILITY.md); checkpoint backups
alone do not provide live telemetry.

## ⌨️ Use it from the terminal

Discover available commands, then select the workspace you want to operate in:

```bash
uv run mfiche --json capabilities
uv run mfiche --json workspace list
uv run mfiche --json --workspace <workspace-id-or-slug> project list
```

The workspace selected in your browser does not select the CLI workspace.
Use an explicit workspace ID or slug for scoped reads and writes.

See the [CLI guide](docs/CLI.md) for examples and the
[agent guide](docs/AGENT_GUIDE.md) for unattended automation.

## 📚 Go deeper

| Guide | What's inside |
| --- | --- |
| [Installation & operations](docs/OPERATIONS.md) | App setup, remote access, storage, credentials, backup and restore |
| [CLI guide](docs/CLI.md) | Commands, common workflows, JSON output, and error handling |
| [Agent guide](docs/AGENT_GUIDE.md) | Automation rules and copy-ready recipes |
| [AI Toolkit & W&B logging](docs/WANDB_COMPATIBILITY.md) | Trainer packets, live metrics, samples, and checkpoint handoff |
| [Contributing](CONTRIBUTING.md) | Development setup, tests, migrations, and release builds |
| [Security](SECURITY.md) | Deployment boundaries and vulnerability reporting |

For architecture, product definitions, and compatibility references, visit the
[documentation index](docs/README.md).
