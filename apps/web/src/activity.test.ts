import { describe, expect, it } from "vitest";

import { activityLabel } from "./activity";

describe("activityLabel", () => {
  it("maps known provider events to concise user-facing text", () => {
    expect(activityLabel("FAL.ADMISSION_ADMITTED")).toBe("Generation admitted");
    expect(activityLabel("fal.admission_admitted")).toBe("Generation admitted");
    expect(activityLabel("FAL.RESULT_INGESTED")).toBe("Generated image ingested");
  });

  it("humanizes unknown event keys without exposing raw machine formatting", () => {
    expect(activityLabel("TRANSFER.OBJECT_UPLOAD_COMPLETED")).toBe("Transfer · Object upload completed");
  });

  it("has a useful fallback for a missing event key", () => {
    expect(activityLabel(undefined)).toBe("Activity recorded");
  });
});
