import { useEffect, useState, type ImgHTMLAttributes } from "react";

import { api } from "./api";

export type AssetImageVariant = "thumbnail" | "content";
type AssetDeliveryResult =
  | { kind: "descriptor"; descriptor: { delivery_url: string; asset_revision_id: string; variant: { kind: AssetImageVariant } } }
  | { kind: "failure"; failure: { redacted_message: string; action: { kind: string } } };

type AssetImageProps = Omit<ImgHTMLAttributes<HTMLImageElement>, "src"> & {
  assetRevisionId: string;
  variant?: AssetImageVariant;
  maxPixels?: 256 | 512 | 1024;
};

/** Resolve the server-owned delivery descriptor before assigning an image URL. */
export function AssetImage({ assetRevisionId, variant = "thumbnail", maxPixels = 512, alt = "", ...props }: AssetImageProps) {
  const [delivery, setDelivery] = useState<{ src: string; error?: string }>();

  useEffect(() => {
    let active = true;
    setDelivery(undefined);
    void api<AssetDeliveryResult>(`/api/assets/${encodeURIComponent(assetRevisionId)}/delivery`, {
      method: "POST",
      body: JSON.stringify({
        asset_revision_id: assetRevisionId,
        variant: variant === "content" ? { kind: "content" } : { kind: "thumbnail", max_pixels: maxPixels },
      }),
    }).then(result => {
      if (!active) return;
      if (result.kind === "descriptor") setDelivery({ src: result.descriptor.delivery_url });
      else setDelivery({ src: "", error: result.failure.redacted_message });
    }).catch(reason => {
      if (active) setDelivery({ src: "", error: reason instanceof Error ? reason.message : String(reason) });
    });
    return () => { active = false; };
  }, [assetRevisionId, maxPixels, variant]);

  return <img {...props} src={delivery?.src || undefined} alt={alt} aria-busy={!delivery} data-delivery-error={delivery?.error || undefined} />;
}
