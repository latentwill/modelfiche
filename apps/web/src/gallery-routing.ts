import { routeQuery, str } from "./api";

type Row = Record<string, unknown>;

export function safeReturnTarget(value: string | null) {
  if (!value || value.startsWith("/") || value.startsWith("#") || /^[a-z]+:/i.test(value)) return "";
  return value;
}

export function galleryOpenHref(asset: Row, category = "") {
  const metadata = asset.metadata && typeof asset.metadata === "object" ? asset.metadata as Row : {};
  const origin = str(asset.origin_type ?? metadata.origin_type, "").toUpperCase();
  const resolvedCategory = category || (origin === "EVAL" ? "eval_output" : origin === "SAMPLE" ? "sample" : origin === "DATASET" ? "dataset_image" : "");
  const returnTarget = typeof location !== "undefined" ? safeReturnTarget(location.hash.replace(/^#\/?/, "")) : "";
  return `#/gallery${routeQuery({ project: asset.project_id ?? metadata.project_id, dataset: asset.dataset_id ?? metadata.dataset_id, eval: asset.eval_run_id ?? metadata.eval_run_id, category: resolvedCategory, include_dataset_assets: resolvedCategory === "dataset_image" ? true : "", asset: asset.asset_revision_id ?? asset.asset_id ?? asset.id, return: returnTarget })}`;
}
