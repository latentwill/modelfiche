import { describe, expect, it } from "vitest";

import { activityDisplayText, activityLabel, activityObjectHref, previewAssetId } from "./activity";

describe("activityLabel", () => {
  it("maps generation completion to image language", () => {
    expect(activityLabel("asset.generated")).toBe("Image Generated");
    expect(activityLabel("image.generated")).toBe("Image Generated");
    expect(activityLabel("FAL.ADMISSION_ADMITTED")).toBe("Generation admitted");
  });

  it("humanizes unknown event keys without exposing raw machine formatting", () => {
    expect(activityLabel("TRANSFER.OBJECT_UPLOAD_COMPLETED")).toBe("Transfer · Object upload completed");
  });

  it("has a useful fallback for a missing event key", () => {
    expect(activityLabel(undefined)).toBe("Activity recorded");
  });
});

describe("activity display text", () => {
  it("replaces image filenames while preserving useful object labels", () => {
    expect(activityDisplayText("Generated abc123.png", "Generated image")).toBe("Generated image");
    expect(activityDisplayText("step 3750", "Generated image")).toBe("step 3750");
    expect(activityDisplayText("", "Generated image")).toBe("Generated image");
  });
});

describe("activity row links and previews", () => {
  it("returns only non-empty API-provided hrefs", () => {
    expect(activityObjectHref("#/image/image-1")).toBe("#/image/image-1");
    expect(activityObjectHref("  #/run/run-1  ")).toBe("#/run/run-1");
    expect(activityObjectHref("")).toBeNull();
    expect(activityObjectHref(null)).toBeNull();
    expect(activityObjectHref(42)).toBeNull();
    expect(activityObjectHref("/image/image-1")).toBeNull();
  });

  it("returns only non-empty preview asset identifiers", () => {
    expect(previewAssetId("asset-revision-1")).toBe("asset-revision-1");
    expect(previewAssetId("  asset-revision-2  ")).toBe("asset-revision-2");
    expect(previewAssetId("")).toBeNull();
    expect(previewAssetId(undefined)).toBeNull();
    expect(previewAssetId({ id: "asset-revision-3" })).toBeNull();
  });
});
