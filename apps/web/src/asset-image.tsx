import { useEffect, useRef, useState, type ImgHTMLAttributes } from "react";

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

const descriptorCache = new Map<string, string>();
const descriptorRequests = new Map<string, Promise<string>>();

function descriptorKey(assetRevisionId: string, variant: AssetImageVariant, maxPixels: number) {
  return `${assetRevisionId}:${variant}:${variant === "thumbnail" ? maxPixels : "original"}`;
}

async function resolveDelivery(assetRevisionId: string, variant: AssetImageVariant, maxPixels: number, fresh = false) {
  const key = descriptorKey(assetRevisionId, variant, maxPixels);
  if (fresh) { descriptorCache.delete(key); descriptorRequests.delete(key); }
  const cached = descriptorCache.get(key);
  if (cached) return cached;
  const pending = descriptorRequests.get(key);
  if (pending) return pending;
  const request = api<AssetDeliveryResult>(`/api/assets/${encodeURIComponent(assetRevisionId)}/delivery`, {
    method: "POST",
    body: JSON.stringify({ asset_revision_id: assetRevisionId, variant: variant === "content" ? { kind: "content" } : { kind: "thumbnail", max_pixels: maxPixels } }),
  }).then(result => {
    if (result.kind === "failure") throw new Error(result.failure.redacted_message);
    descriptorCache.set(key, result.descriptor.delivery_url);
    return result.descriptor.delivery_url;
  }).finally(() => descriptorRequests.delete(key));
  descriptorRequests.set(key, request);
  return request;
}

/** Resolve the server-owned delivery descriptor before assigning an image URL. */
export function AssetImage({ assetRevisionId, variant = "thumbnail", maxPixels = 512, alt = "", ...props }: AssetImageProps) {
  const [delivery, setDelivery] = useState<{ src: string; error?: string }>();
  const [visible, setVisible] = useState(props.loading !== "lazy");
  const imageRef = useRef<HTMLImageElement>(null);
  const retriedRef = useRef(false);

  useEffect(() => {
    if (visible || props.loading !== "lazy" || typeof IntersectionObserver === "undefined") { setVisible(true); return; }
    const image = imageRef.current;
    if (!image) return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { setVisible(true); observer.disconnect(); }
    }, { rootMargin: "300px" });
    observer.observe(image);
    return () => observer.disconnect();
  }, [props.loading, visible]);

  useEffect(() => {
    let active = true;
    retriedRef.current = false;
    setDelivery(undefined);
    if (!visible || !assetRevisionId) return () => { active = false; };
    void resolveDelivery(assetRevisionId, variant, maxPixels).then(src => {
      if (active) setDelivery({ src });
    }).catch(reason => {
      if (active) setDelivery({ src: "", error: reason instanceof Error ? reason.message : String(reason) });
    });
    return () => { active = false; };
  }, [assetRevisionId, maxPixels, variant, visible]);

  return <img {...props} ref={imageRef} src={delivery?.src || undefined} alt={alt} aria-busy={visible && !delivery} data-delivery-error={delivery?.error || undefined} onError={event => {
    props.onError?.(event);
    if (!delivery?.src || retriedRef.current) return;
    retriedRef.current = true;
    setDelivery(undefined);
    void resolveDelivery(assetRevisionId, variant, maxPixels, true).then(src => setDelivery({ src })).catch(reason => setDelivery({ src: "", error: reason instanceof Error ? reason.message : String(reason) }));
  }} />;
}
