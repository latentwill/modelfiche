import { describe, expect, it } from "vitest";
import { buildExperimentCreatePayload, buildExperimentPlanPayload, buildExperimentPreflightPayload, buildExperimentQueuePayload, resolveExperimentCells, validateExperimentPlan, type ExperimentPlan } from "./experiment-plan";

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
  it("builds a queue payload from the resolved plan", () => {
    expect(buildExperimentQueuePayload("i", 2, "sha256:x", [{ ordinal: 0 }, { ordinal: 1 }])).toEqual({ idempotency_key: "i", plan_version: 2, plan_digest: "sha256:x", cell_ordinals: [0, 1], billing_acknowledgement: "I understand FAL may bill this run even if checkpoint fetch fails" });
  });
});

it("resolves fixed prompt across seed and scale axes without multiplying context", () => {
  const fixed: ExperimentPlan = {
    contract_version: "2026-07-22.v1", project_id: "p",
    axes: { x: { name: "seed", values: [{ value: 11 }, { value: 22 }] }, y: { name: "lora_scale", values: [{ value: 0.5 }, { value: 1 }] }, z: null },
    cases: [{ case_id: "c", ordinal: 0, input: { prompt: "fixed" } }],
    targets: [{ target_id: "t", ordinal: 0, provider: "fal", endpoint_id: "i", model_version_id: "m", checkpoint_revision_id: "r", schema: { defaults: {} } }],
    fixed_case_id: "c", fixed_target_id: "t", shared_params: {}, endpoint_defaults: {},
  };
  const cells = resolveExperimentCells(fixed);
  expect(cells).toHaveLength(4);
  expect(cells.map(cell => [cell.effective_params.seed, cell.effective_params.lora_scale])).toEqual([[11, 0.5], [11, 1], [22, 0.5], [22, 1]]);
});

it("resolves a Z Case axis against a Target axis", () => {
  const base = plan();
  const zCase: ExperimentPlan = { ...base, axes: { x: { name: "seed", values: [{ value: 1 }] }, y: { name: "target", values: [{ value: "t1" }, { value: "t2" }] }, z: { name: "case", values: [{ value: "c1" }, { value: "c2" }] } } };
  expect(resolveExperimentCells(zCase).map(cell => `${cell.target.target_id}:${cell.case.case_id}`)).toEqual(["t1:c1", "t1:c2", "t2:c1", "t2:c2"]);
});

it("sanitizes the exact PlanPayload shape including endpoint-only targets", () => {
  const value = buildExperimentPlanPayload({
    contract_version: "2026-07-22.v1", project_id: "p",
    axes: { x: { name: "target", values: [{ value: { value: "t-base" } }] }, y: { name: "case", values: [{ value: "c" }] }, z: null },
    cases: [{ case_id: "c", ordinal: 0, input: { prompt: "p" }, input_digest: "sha256:stale" }],
    targets: [{ target_id: "t-base", ordinal: 0, provider: "fal", endpoint_id: "ideogram/v4/lora", model_version_id: null, checkpoint_revision_id: null, schema: { fields: { secret: {} } }, shared_overrides: { acceleration: "none" }, target_overrides: { acceleration: "regular" } }],
    shared_params: { num_images: 1 }, endpoint_defaults: {},
  });
  expect(value).toEqual({
    contract_version: "2026-07-22.v1",
    axes: { x: { name: "target", values: ["t-base"] }, y: { name: "case", values: ["c"] }, z: null },
    cases: [{ case_id: "c", ordinal: 0, input: { prompt: "p" } }],
    targets: [{ target_id: "t-base", ordinal: 0, provider: "fal", endpoint_id: "ideogram/v4/lora", model_version_id: null, checkpoint_revision_id: null, overrides: { acceleration: "regular" } }],
    shared_params: { num_images: 1 },
  });
  expect(JSON.stringify(value)).not.toContain("schema");
});

it("builds exact preflight and create envelopes without client-only snapshots", () => {
  const plan: ExperimentPlan = {
    contract_version: "2026-07-22.v1", project_id: "p",
    axes: { x: { name: "seed", values: [{ value: 1 }] }, y: { name: "case", values: [{ value: "c" }] }, z: null },
    cases: [{ case_id: "c", ordinal: 0, input: { prompt: "fixed" } }],
    targets: [{ target_id: "t", ordinal: 0, provider: "fal", endpoint_id: "ideogram/v4/lora", model_version_id: null, checkpoint_revision_id: null, schema: { fields: { acceleration: { type: "enum" } } }, target_overrides: {} }],
    fixed_case_id: "c", fixed_target_id: "t", shared_params: {}, endpoint_defaults: {},
  };
  const preflight = buildExperimentPreflightPayload("preflight-attempt", plan);
  expect(preflight).toMatchObject({ idempotency_key: "preflight-attempt", axes: { x: { name: "seed", values: [1] }, y: { name: "case", values: ["c"] }, z: null }, fixed_target_id: "t" });
  const create = buildExperimentCreatePayload("Grid", "create-attempt", plan);
  expect(create).toMatchObject({ name: "Grid", idempotency_key: "create-attempt", plan: { shared_params: {} } });
  expect(JSON.stringify(create)).not.toContain("schema");
});
