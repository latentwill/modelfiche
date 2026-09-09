from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.database import Base
from titles_worker.sqlalchemy_eval_sink import SQLAlchemyEvalOutputSink


def test_grid_status_scopes_selected_queue_cells_and_aggregates_partial():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        workspace = models.Workspace(name="workspace")
        project = models.Project(workspace_id=workspace.id, title="project")
        plan_snapshot = {"contract_version": "2026-07-22.v1", "project_id": project.id, "plan_version": 1}
        plan = models.ExperimentPlan(project_id=project.id, contract_version="2026-07-22.v1", plan_version=1, digest="a" * 64, snapshot=plan_snapshot)
        definition = models.EvalDefinition(project_id=project.id, name="eval", endpoint="mixed", plan_id=plan.id, plan_version=1, plan_digest=plan.digest)
        grid = models.GridDefinition(project_id=project.id, name="grid", eval_definition_id=definition.id, plan_id=plan.id, plan_version=1, plan_digest=plan.digest, plan_snapshot=plan_snapshot)
        session.add_all([workspace, project, plan, definition, grid])
        session.flush()
        run = models.EvalRun(definition_id=definition.id, plan_id=plan.id, plan_version=1, plan_digest=plan.digest, status="running")
        session.add(run)
        cells = [models.GridCell(grid_definition_id=grid.id, ordinal=i, x_index=i, y_index=0, coordinate={"x": i, "y": 0, "z": None}, status="pending") for i in range(3)]
        session.add_all(cells)
        session.flush()
        item = models.GenerationQueueItem(workspace_id=workspace.id, project_id=project.id, workflow="grid", provider="fal", queue_owner="grid", client_request_id="q", compiled_request_id="compiled-q", request_snapshot={"run_id": run.id, "cells": [{"grid_cell_id": cells[0].id}, {"grid_cell_id": cells[1].id}]})
        item2 = models.GenerationQueueItem(workspace_id=workspace.id, project_id=project.id, workflow="grid", provider="fal", queue_owner="grid", client_request_id="q2", compiled_request_id="compiled-q2", request_snapshot={"run_id": run.id, "cells": [{"grid_cell_id": cells[2].id}]})
        session.add_all([item, item2])
        session.flush()
        session.add_all([models.GenerationQueueChild(queue_item_id=item.id, ordinal=i, compiled_request_id=f"child-{i}", state="succeeded", result={"grid_cell_id": cells[i].id}) for i in range(2)])
        session.add(models.GenerationQueueChild(queue_item_id=item2.id, ordinal=0, compiled_request_id="child-2", state="admitted", result={"grid_cell_id": cells[2].id}))
        run_id, grid_id, item_id, cell_ids = run.id, grid.id, item.id, [cell.id for cell in cells]
    sink = SQLAlchemyEvalOutputSink(factory, cache=object())
    with factory.begin() as session:
        for cell_id in cell_ids[:2]:
            cell = session.get(models.GridCell, cell_id)
            cell.status = "succeeded"
            cell.eval_output_id = "output-" + cell_id
    sink.mark_succeeded(run_id)
    with Session(engine) as session:
        assert session.get(models.GenerationQueueItem, item_id).state == "succeeded"
        assert session.get(models.GridDefinition, grid_id).status == "partial"
        assert session.get(models.GridCell, cell_ids[2]).status == "pending"
    sink.mark_failed(run_id, "selected failure")
    with Session(engine) as session:
        assert session.get(models.GridCell, cell_ids[2]).status == "failed"
        assert all(session.get(models.GridCell, cell_id).status == "succeeded" for cell_id in cell_ids[:2])
