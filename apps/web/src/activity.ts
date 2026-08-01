const ACTIVITY_LABELS: Record<string, string> = {
  "ASSET.GENERATED": "Image Generated",
  "IMAGE.GENERATED": "Image Generated",
  "FAL.ADMISSION_ADMITTED": "Generation admitted",
  "FAL.ADMISSION_REJECTED": "Generation rejected",
  "FAL.REQUEST_SUBMITTED": "Generation submitted",
  "FAL.REQUEST_RUNNING": "Generation running",
  "FAL.REQUEST_COMPLETED": "Generation completed",
  "FAL.REQUEST_FAILED": "Generation failed",
  "FAL.RESULT_INGESTED": "Generated image ingested",
};

export function activityLabel(value: unknown) {
  const key = String(value ?? "").trim();
  if (!key) return "Activity recorded";
  if (ACTIVITY_LABELS[key.toUpperCase()]) return ACTIVITY_LABELS[key.toUpperCase()];
  return key
    .split(".")
    .filter(Boolean)
    .map(part => {
      const words = part.toLowerCase().replaceAll("_", " ");
      return words.charAt(0).toUpperCase() + words.slice(1);
    })
    .join(" · ");
}
export function activityObjectHref(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const href = value.trim();
  return href.startsWith("#/") ? href : null;
}
export function activityDisplayText(value: unknown, fallback: string) {
  const text = String(value ?? "").trim();
  if (!text || /\.(?:png|jpe?g|webp|gif|avif)(?:$|[?#])/i.test(text)) return fallback;
  return text;
}


export function previewAssetId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const id = value.trim();
  return id || null;
}
