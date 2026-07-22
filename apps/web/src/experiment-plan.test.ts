import { describe, expect, it } from "vitest";
import { buildExperimentQueuePayload, resolveExperimentCells, validateExperimentPlan, type ExperimentPlan } from "./experiment-plan";

  const plan = (targetAxis = true): ExperimentPlan => ({ contract_version: "2026-07-22.v1", project_id: "p", axes: { x: { name: targetAxis ? "target" : "seed", values: targetAxis ? [{ value: { target_id: "t1" } }, { value: { target_id: "t2" } }] : [{ value: 1 }] }, y: { name: "case", values: [{ value: { case_id: "c1" } }, { value: { case_id: "c2" } }] }, z: null }, cases: [{ case_id: "c1", ordinal: 0, input: { prompt: "one" } }, { case_id: "c2", ordinal: 1, input: { prompt: "two" } }], targets: [{ target_id: "t1", ordinal: 0, provider: "fal", endpoint_id: "i", model_version_id: "m1", checkpoint_revision_id: "r1", schema: { defaults: { seed: 1 } }, shared_overrides: { shared: "yes" }, target_overrides: { target: 1 } }, { target_id: "t2", ordinal: 1, provider: "fal", endpoint_id: "k", model_version_id: "m2", checkpoint_revision_id: "r2", schema: { defaults: { seed: 2 } }, shared_overrides: { shared: "yes" }, target_overrides: { target: 2 } }], shared_params: { shared: "shared" }, endpoint_defaults: { i: { endpoint: "i" }, k: { endpoint: "k" } } });

describe("experiment plan resolver", () => {
  it("resolves two targets by two cases exactly once", () => {
    const cells = resolveExperimentCells(plan());
    expect(cells).toHaveLength(4);
    expect(cells.map(cell => `${cell.target.target_id}:${cell.case.case_id}`)).toEqual(["t1:c1", "t1:c2", "t2:c1", "t2:c2"]);
  });
  it("applies endpoint defaults, schema defaults, shared, target and axis precedence", () => {
    const cells = resolveExperimentCells(plan());
    expect(cells[0].effective_params).toMatchObject({ endpoint: "i", seed: 1, shared: "yes", target: 1 });
  });
  it("rejects multiple fixed targets or cases when their axes are absent", () => {
    expect(validateExperimentPlan(plan(false)).some(error => error.includes("Target must be an axis"))).toBe(true);
  });
  it("uses the exact billing acknowledgement queue contract", () => {
    expect(buildExperimentQueuePayload("i", 2, "sha256:x", [{ ordinal: 0 }, { ordinal: 1 }])).toMatchObject({ idempotency_key: "i", plan_version: 2, plan_digest: "sha256:x", cell_ordinals: [0, 1], billing_acknowledgement: "I understand FAL may bill this run even if checkpoint fetch fails" });
  });
});
