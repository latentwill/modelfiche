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
const unavailablePlaceholder = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 120"><rect width="160" height="120" fill="#d1d5db"/><g fill="none" stroke="#6b7280" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"><rect x="35" y="27" width="90" height="66" rx="5"/><circle cx="62" cy="51" r="8"/><path d="m43 84 25-23 17 15 13-12 19 20"/></g></svg>')}`;
const loadingPlaceholder = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 120"><rect width="160" height="120" fill="#f2f4fa"/><g fill="none" stroke="#b8c5ed" stroke-width="1" opacity=".7"><path d="M0 20h160M0 40h160M0 60h160M0 80h160M0 100h160M20 0v120M40 0v120M60 0v120M80 0v120M100 0v120M120 0v120M140 0v120"/></g><g fill="none" stroke="#1237b8"><circle cx="80" cy="60" r="22" stroke-dasharray="2 5"/><path d="M80 29v62M49 60h62M9 22V9h13M138 9h13v13M9 98v13h13M151 98v13h-13"/></g><circle cx="80" cy="60" r="3" fill="#1237b8"/></svg>')}`;

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

export { resolveDelivery as resolveAssetDelivery };

/** Resolve the server-owned delivery descriptor before assigning an image URL. */
export function AssetImage({ assetRevisionId, variant = "thumbnail", maxPixels = 512, alt = "", className, ...props }: AssetImageProps) {
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

  const deliveryState = delivery?.error ? "unavailable" : delivery?.src ? "loaded" : "loading";
  return <img {...props} ref={imageRef} className={`asset-image asset-image-${deliveryState}${className ? ` ${className}` : ""}`} src={delivery?.src || (delivery?.error ? unavailablePlaceholder : loadingPlaceholder)} alt={alt} aria-busy={deliveryState === "loading"} data-delivery-state={deliveryState} data-delivery-error={delivery?.error || undefined} onError={event => {
    props.onError?.(event);
    if (!delivery?.src || retriedRef.current) return;
    retriedRef.current = true;
    setDelivery(undefined);
    void resolveDelivery(assetRevisionId, variant, maxPixels, true).then(src => setDelivery({ src })).catch(reason => setDelivery({ src: "", error: reason instanceof Error ? reason.message : String(reason) }));
  }} />;
}
