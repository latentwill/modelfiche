from types import SimpleNamespace

from sqlalchemy.orm import sessionmaker

from titles_api import models
from titles_api.database import Base, build_engine
from titles_worker.fal_jobs import FalGridBatchHandler
from titles_worker.runner import JobContext


class FakeClient:
    def __init__(self):
        self.submissions = []

    def submit(self, endpoint, request):
        index = len(self.submissions)
        self.submissions.append((endpoint, request))
        return SimpleNamespace(request_id=f"request-{index}", status_url=f"status-{index}", response_url=f"response-{index}", cancel_url=f"cancel-{index}")

    def status(self, _url):
        return {"status": "COMPLETED"}

    def result(self, url):
        return {"images": [{"url": url}]}

    def cancel(self, _url):
        raise AssertionError("cancel should not be called")


class FakeSink:
    def __init__(self):
        self.cells = set()
        self.output = 0
        self.state = None

    def provider_submitted(self, _eval_run_id, _request_id):
        self.state = "running"

    def ingest_outputs(self, _eval_run_id, _prompt_id, _response):
        self.output += 1
        return {"outputs": 1, "output_ids": [f"output-{self.output}"]}

    def link_grid_cell(self, grid_id, output_id, x_index, y_index):
        self.cells.add((grid_id, x_index, y_index, output_id))

    def grid_cell_exists(self, grid_id, x_index, y_index):
        return any(cell[:3] == (grid_id, x_index, y_index) for cell in self.cells)

    def mark_succeeded(self, _eval_run_id):
        self.state = "succeeded"

    def mark_failed(self, _eval_run_id, _message):
        self.state = "failed"

    def mark_canceled(self, _eval_run_id):
        self.state = "canceled"


def test_grid_batch_persists_each_cell_and_resumes_without_resubmission(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'grid.sqlite3'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as db:
        workspace = models.Workspace(name="Test")
        db.add(workspace)
        db.flush()
        profile = models.UserProfile(workspace_id=workspace.id, display_name="Operator")
        project = models.Project(workspace_id=workspace.id, title="Project")
        db.add_all([profile, project])
        db.flush()
        asset = models.Asset(workspace_id=workspace.id, project_id=project.id, kind=models.AssetKind.model, name="step-100.safetensors")
        run = models.TrainingRun(project_id=project.id, name="Run")
        prompt_set = models.PromptSet(project_id=project.id, name="Prompts")
        model = models.Model(project_id=project.id, name="Model")
        db.add_all([asset, run, prompt_set, model])
        db.flush()
        checkpoint = models.Checkpoint(run_id=run.id, step=100, asset_id=asset.id)
        prompt = models.Prompt(prompt_set_id=prompt_set.id, text="portrait", position=0)
        db.add_all([checkpoint, prompt])
        db.flush()
        version = models.ModelVersion(model_id=model.id, checkpoint_id=checkpoint.id, name="v1")
        db.add(version)
        db.flush()
        definition = models.EvalDefinition(project_id=project.id, name="Eval", endpoint="ideogram/v4/lora", model_version_id=version.id, prompt_set_id=prompt_set.id, parameters={"image_size": "square_hd"})
        db.add(definition)
        db.flush()
        grid = models.GridDefinition(project_id=project.id, name="Scale", eval_definition_id=definition.id, x_axis={"parameter": "seed", "values": [1, 2]}, y_axis={"parameter": "lora_scale", "values": [0.8, 1.0]})
        db.add(grid)
        db.flush()
        job = models.Job(workspace_id=workspace.id, profile_id=profile.id, kind="fal.grid_batch", payload={"grid_definition_id": grid.id})
        db.add(job)
        db.flush()
        grid_id, job_id = grid.id, job.id

    client, sink = FakeClient(), FakeSink()
    resolved = []

    def resolver(payload):
        resolved.append(payload)
        return {"prompt": payload["prompt"], "seed": payload["parameters"].get("seed"), "scale": payload["lora_scale"]}

    handler = FalGridBatchHandler(sessions, client, sink, resolver, poll_seconds=0)
    result = handler(JobContext(sessions, job_id), {"grid_definition_id": grid_id})
    assert result["completed_cells"] == 4
    assert len(client.submissions) == 4
    assert {(item["parameters"]["seed"], item["lora_scale"]) for item in resolved} == {(1, 0.8), (1, 1.0), (2, 0.8), (2, 1.0)}
    with sessions() as db:
        persisted = dict(db.get(models.Job, job_id).payload)
        assert persisted["eval_run_id"]
        assert len(persisted["grid_tasks"]) == 4

    resumed = handler(JobContext(sessions, job_id), persisted)
    assert resumed["completed_cells"] == 4
    assert len(client.submissions) == 4
    assert sink.state == "succeeded"
