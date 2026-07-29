export type JsonObject = Record<string, unknown>;

export type PlanAxisValue = { value: unknown; digest?: string };
export type PlanAxis = { name: string; values: PlanAxisValue[] };
export type ExperimentCase = {
  case_id: string;
  ordinal: number;
  input: { prompt?: string; variables?: JsonObject; [key: string]: unknown };
  input_digest?: string;
};
export type ExperimentTarget = {
  target_id: string;
  ordinal: number;
  provider: string;
  endpoint_id: string;
  model_version_id?: string | null;
  checkpoint_revision_id?: string | null;
  display_name?: string;
  checkpoint_step?: number;
  schema_digest?: string;
  schema: JsonObject;
  fixed_target?: JsonObject;
  shared_overrides?: JsonObject;
  target_overrides?: JsonObject;
};
export type ExperimentPlan = {
  contract_version: "2026-07-22.v1";
  project_id: string;
  plan_id?: string;
  plan_version?: number;
  digest?: string;
  axes: { x: PlanAxis; y: PlanAxis; z: PlanAxis | null };
  cases: ExperimentCase[];
  targets: ExperimentTarget[];
  fixed_case_id?: string;
  fixed_target_id?: string;
  shared_params: JsonObject;
  endpoint_defaults: JsonObject;
};
export type ResolvedCell = {
  ordinal: number;
  coordinate: { x: number; y: number; z: number | null };
  case: ExperimentCase;
  target: ExperimentTarget;
  effective_params: JsonObject;
  request_count: number;
  estimated_cost: string;
  schema_digest?: string;
  status: "admissible" | "invalid";
  errors: string[];
};

/** Canonical JSON used by plan/case/axis digest producers. */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  return `{${Object.keys(value as JsonObject).sort().map(key => `${JSON.stringify(key)}:${canonicalJson((value as JsonObject)[key])}`).join(",")}}`;
}

export async function sha256Digest(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalJson(value));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return `sha256:${Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("")}`;
}

function unwrappedAxisValue(value: unknown): unknown {
  return value && typeof value === "object" && "value" in value ? (value as JsonObject).value : value;
}

function mergeParams(...values: Array<JsonObject | undefined>): JsonObject {
  return Object.assign({}, ...values.filter((value): value is JsonObject => Boolean(value)));
}

function axisObject(value: unknown): JsonObject {
  return value && typeof value === "object" ? value as JsonObject : {};
}

function selectedAxisId(value: unknown, kind: "target" | "case"): string {
  const object = axisObject(unwrappedAxisValue(value));
  return String(object[`${kind}_id`] ?? object.id ?? object.value ?? unwrappedAxisValue(value) ?? "");
}

function endpointFields(schema: JsonObject): { required: string[]; properties: JsonObject } {
  if (schema.properties && typeof schema.properties === "object") {
    return { required: Array.isArray(schema.required) ? schema.required.map(String) : [], properties: schema.properties as JsonObject };
  }
  const defaults = schema.defaults && typeof schema.defaults === "object" ? schema.defaults as JsonObject : {};
  const fields = schema.fields && typeof schema.fields === "object" ? schema.fields as JsonObject : {};
  const requestFields = Array.isArray(schema.request_fields) ? schema.request_fields.map(String) : [];
  return {
    required: requestFields.filter(key => fields[key] && typeof fields[key] === "object" && (fields[key] as JsonObject).required === true),
    properties: Object.fromEntries(Array.from(new Set([...Object.keys(defaults), ...requestFields])).map(key => {
      const field = fields[key] && typeof fields[key] === "object" ? fields[key] as JsonObject : {};
      const type = field.type === "integer" || field.type === "number" ? field.type : field.type === "boolean" ? "boolean" : field.type === "enum" ? undefined : "string";
      return [key, { ...field, type, enum: Array.isArray(field.options) ? field.options : field.enum }];
    })),
  };
}

function schemaErrors(value: JsonObject, schema: JsonObject): string[] {
  const errors: string[] = [];
  const normalized = endpointFields(schema);
  for (const key of normalized.required) if (!(key in value)) errors.push(`${key} is required.`);
  for (const [key, definitionValue] of Object.entries(normalized.properties)) {
    if (!(key in value) || !definitionValue || typeof definitionValue !== "object") continue;
    const definition = definitionValue as JsonObject;
    const actual = value[key];
    const options = Array.isArray(definition.enum) ? definition.enum : [];
    if (options.length && !options.some(option => canonicalJson(option) === canonicalJson(actual))) errors.push(`${key} is not an allowed value.`);
    if (definition.type === "string" && typeof actual !== "string") errors.push(`${key} must be a string.`);
    if (definition.type === "boolean" && typeof actual !== "boolean") errors.push(`${key} must be a boolean.`);
    if (definition.type === "number" && (typeof actual !== "number" || !Number.isFinite(actual))) errors.push(`${key} must be a number.`);
    if (definition.type === "integer" && (typeof actual !== "number" || !Number.isInteger(actual))) errors.push(`${key} must be an integer.`);
    if (definition.type === "image_size" && typeof actual !== "string" && (!actual || typeof actual !== "object" || !Number.isInteger((actual as JsonObject).width) || !Number.isInteger((actual as JsonObject).height))) errors.push(`${key} must be a supported image size.`);
    const minimum = definition.minimum ?? definition.min;
    const maximum = definition.maximum ?? definition.max;
    if (typeof actual === "number") {
      if (typeof minimum === "number" && actual < minimum) errors.push(`${key} is below its minimum.`);
      if (typeof maximum === "number" && actual > maximum) errors.push(`${key} exceeds its maximum.`);
    }
  }
  return errors;
}

function axisSelection(axis: PlanAxis | null, ids: Set<string>, kind: "target" | "case"): Set<string> | null {
  if (!axis) return null;
  const names = kind === "target" ? ["target"] : ["case", "prompt"];
  if (!names.includes(axis.name.toLowerCase())) return null;
  const selected = new Set<string>();
  for (const entry of axis.values) {
    const raw = entry.value && typeof entry.value === "object" && "value" in (entry.value as JsonObject) ? (entry.value as JsonObject).value : entry.value;
    const object = raw && typeof raw === "object" ? raw as JsonObject : {};
    const id = String(object[`${kind}_id`] ?? object.id ?? object.value ?? raw ?? "");
    if (ids.has(id)) selected.add(id);
  }
  return selected;
}
export function validateExperimentPlan(plan: ExperimentPlan): string[] {
  const errors: string[] = [];
  const axisNames = [plan.axes.x.name, plan.axes.y.name, ...(plan.axes.z ? [plan.axes.z.name] : [])];
  if (new Set(axisNames).size !== axisNames.length) errors.push("Each axis must use a different field.");
  const targetIds = new Set(plan.targets.map(target => target.target_id));
  const caseIds = new Set(plan.cases.map(item => item.case_id));
  const targetAxis = axisSelection(plan.axes.x, targetIds, "target") ?? axisSelection(plan.axes.y, targetIds, "target") ?? axisSelection(plan.axes.z, targetIds, "target");
  const caseAxis = axisSelection(plan.axes.x, caseIds, "case") ?? axisSelection(plan.axes.y, caseIds, "case") ?? axisSelection(plan.axes.z, caseIds, "case");
  if (!targetAxis && plan.targets.length > 1) errors.push("Target must be an axis when more than one target is selected.");
  if (!caseAxis && plan.cases.length > 1) errors.push("Case must be an axis when more than one case is selected.");
  if (targetAxis && targetAxis.size !== plan.targets.length) errors.push("Target axis must select every selected target exactly once.");
  if (caseAxis && caseAxis.size !== plan.cases.length) errors.push("Case axis must select every selected case exactly once.");
  return errors;
}

/** Resolve every deterministic cell without enqueueing or mutating a plan. */
export function resolveExperimentCells(plan: ExperimentPlan, requestCount = 1, estimatedCost = "0.000000"): ResolvedCell[] {
  const planErrors = validateExperimentPlan(plan);
  if (planErrors.length) return [];
  const zValues = plan.axes.z?.values ?? [{ value: null }];
  const targetIds = new Set(plan.targets.map(target => target.target_id));
  const caseIds = new Set(plan.cases.map(item => item.case_id));
  const targetAxis = axisSelection(plan.axes.x, targetIds, "target") ?? axisSelection(plan.axes.y, targetIds, "target") ?? axisSelection(plan.axes.z, targetIds, "target");
  const caseAxis = axisSelection(plan.axes.x, caseIds, "case") ?? axisSelection(plan.axes.y, caseIds, "case") ?? axisSelection(plan.axes.z, caseIds, "case");
  const chosenTargets = plan.targets.filter(target => targetAxis ? targetAxis.has(target.target_id) : target.target_id === plan.fixed_target_id || plan.targets.length === 1);
  const chosenCases = plan.cases.filter(item => caseAxis ? caseAxis.has(item.case_id) : item.case_id === plan.fixed_case_id || plan.cases.length === 1);
  const cells: ResolvedCell[] = [];
  for (let x = 0; x < plan.axes.x.values.length; x += 1) for (let y = 0; y < plan.axes.y.values.length; y += 1) for (let z = 0; z < zValues.length; z += 1) {
    const coordinateEntries: Array<{ axis: PlanAxis; entry: PlanAxisValue; index: number }> = [
      { axis: plan.axes.x, entry: plan.axes.x.values[x], index: x },
      { axis: plan.axes.y, entry: plan.axes.y.values[y], index: y },
      ...(plan.axes.z ? [{ axis: plan.axes.z, entry: zValues[z], index: z }] : []),
    ];
    const axisValue: JsonObject = {};
    for (const { axis, entry } of coordinateEntries) {
      const raw = entry.value && typeof entry.value === "object" && "value" in (entry.value as JsonObject) ? (entry.value as JsonObject).value : entry.value;
      if (axis.name !== "target" && axis.name !== "case" && axis.name !== "prompt") axisValue[axis.name] = raw;
    }
    const targetEntry = coordinateEntries.find(({ entry }) => {
      const raw = entry.value && typeof entry.value === "object" && "value" in (entry.value as JsonObject) ? (entry.value as JsonObject).value : entry.value;
      return targetIds.has(String(raw && typeof raw === "object" ? (raw as JsonObject).target_id ?? (raw as JsonObject).id : raw));
    });
    const caseEntry = coordinateEntries.find(({ entry }) => {
      const raw = entry.value && typeof entry.value === "object" && "value" in (entry.value as JsonObject) ? (entry.value as JsonObject).value : entry.value;
      return caseIds.has(String(raw && typeof raw === "object" ? (raw as JsonObject).case_id ?? (raw as JsonObject).id : raw));
    });
    const targetRaw = targetEntry ? targetEntry.entry.value && typeof targetEntry.entry.value === "object" && "value" in (targetEntry.entry.value as JsonObject) ? (targetEntry.entry.value as JsonObject).value : targetEntry.entry.value : undefined;
    const caseRaw = caseEntry ? caseEntry.entry.value && typeof caseEntry.entry.value === "object" && "value" in (caseEntry.entry.value as JsonObject) ? (caseEntry.entry.value as JsonObject).value : caseEntry.entry.value : undefined;
    const cellsTargets = targetRaw ? chosenTargets.filter(target => target.target_id === String(targetRaw && typeof targetRaw === "object" ? (targetRaw as JsonObject).target_id ?? (targetRaw as JsonObject).id : targetRaw)) : chosenTargets;
    const cellsCases = caseRaw ? chosenCases.filter(item => item.case_id === String(caseRaw && typeof caseRaw === "object" ? (caseRaw as JsonObject).case_id ?? (caseRaw as JsonObject).id : caseRaw)) : chosenCases;
    for (const caseSnapshot of cellsCases) for (const target of cellsTargets) {
      const schemaDefaults = target.schema.defaults && typeof target.schema.defaults === "object" ? target.schema.defaults as JsonObject : {};
      const endpointDefaults = plan.endpoint_defaults[target.endpoint_id] && typeof plan.endpoint_defaults[target.endpoint_id] === "object" ? plan.endpoint_defaults[target.endpoint_id] as JsonObject : {};
      const effective = mergeParams(endpointDefaults, schemaDefaults, plan.shared_params, target.shared_overrides, target.target_overrides, axisValue);
      const errors = schemaErrors(effective, target.schema);
      cells.push({ ordinal: cells.length, coordinate: { x, y, z: plan.axes.z ? z : null }, case: caseSnapshot, target, effective_params: effective, request_count: requestCount, estimated_cost: estimatedCost, schema_digest: target.schema_digest, status: errors.length ? "invalid" : "admissible", errors });
    }
  }
  return cells;
}

export function buildExperimentPlanPayload(plan: ExperimentPlan) {
  const isAxis = (kind: "target" | "case") => {
    const names = kind === "target" ? ["target"] : ["case", "prompt"];
    return [plan.axes.x, plan.axes.y, plan.axes.z].some(axis => axis && names.includes(axis.name.toLowerCase()));
  };
  return {
    contract_version: plan.contract_version,
    axes: {
      x: { name: plan.axes.x.name, values: plan.axes.x.values.map(entry => entry.value && typeof entry.value === "object" && "value" in (entry.value as JsonObject) ? (entry.value as JsonObject).value : entry.value) },
      y: { name: plan.axes.y.name, values: plan.axes.y.values.map(entry => entry.value && typeof entry.value === "object" && "value" in (entry.value as JsonObject) ? (entry.value as JsonObject).value : entry.value) },
      z: plan.axes.z ? { name: plan.axes.z.name, values: plan.axes.z.values.map(entry => entry.value && typeof entry.value === "object" && "value" in (entry.value as JsonObject) ? (entry.value as JsonObject).value : entry.value) } : null,
    },
    cases: plan.cases.map(({ case_id, ordinal, input }) => ({ case_id, ordinal, input })),
    targets: plan.targets.map(({ target_id, ordinal, provider, endpoint_id, model_version_id, checkpoint_revision_id, shared_overrides, target_overrides }) => ({
      target_id,
      ordinal,
      provider,
      endpoint_id,
      model_version_id: model_version_id ?? null,
      checkpoint_revision_id: checkpoint_revision_id ?? null,
      overrides: { ...(shared_overrides ?? {}), ...(target_overrides ?? {}) },
    })),
    ...(isAxis("case") ? {} : { fixed_case_id: plan.fixed_case_id }),
    ...(isAxis("target") ? {} : { fixed_target_id: plan.fixed_target_id }),
    shared_params: plan.shared_params,
  };
}

export function buildExperimentPreflightPayload(idempotencyKey: string, plan: ExperimentPlan) {
  return { ...buildExperimentPlanPayload(plan), idempotency_key: idempotencyKey };
}

export function buildExperimentCreatePayload(name: string, idempotencyKey: string, plan: ExperimentPlan) {
  return { name, idempotency_key: idempotencyKey, plan: buildExperimentPlanPayload(plan) };
}

export function buildExperimentQueuePayload(idempotencyKey: string, planVersion: unknown, planDigest: unknown, cells: Array<{ ordinal?: unknown }>) {
  return {
    idempotency_key: idempotencyKey,
    plan_version: planVersion,
    plan_digest: planDigest,
    cell_ordinals: cells.map(cell => Number(cell.ordinal)),
    billing_acknowledgement: "I understand FAL may bill this run even if checkpoint fetch fails" as const,
  };
}
