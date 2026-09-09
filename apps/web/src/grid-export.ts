export type GridExportSize = "compact" | "full";

export type GridExportEntry = {
  label: string;
  detail?: string;
};

export type GridExportCell = {
  assetRevisionId?: string;
  status?: string;
  alt?: string;
};

export type GridExportSlice = {
  title?: string;
  cornerLabel: string;
  columns: GridExportEntry[];
  rows: GridExportEntry[];
  cells: GridExportCell[][];
};

type AssetVariant = "thumbnail" | "content";
type ImageResolver = (assetRevisionId: string, variant: AssetVariant, maxPixels: number) => Promise<string>;

type LoadedImage = {
  source: CanvasImageSource;
  width: number;
  height: number;
  close: () => void;
};

type ExportOptions = {
  title: string;
  subtitle: string;
  filename: string;
  size: GridExportSize;
  slices: GridExportSlice[];
  resolveImage: ImageResolver;
};

type Palette = {
  background: string;
  surface: string;
  soft: string;
  media: string;
  border: string;
  ink: string;
  muted: string;
};

type Metrics = {
  margin: number;
  gap: number;
  padding: number;
  cellWidth: number;
  cellHeight: number;
  labelWidth: number;
  titleFont: string;
  subtitleFont: string;
  sliceFont: string;
  labelFont: string;
  detailFont: string;
  lineHeight: number;
  detailLineHeight: number;
};

type SliceLayout = {
  slice: GridExportSlice;
  titleHeight: number;
  titleLines: string[];
  headerHeight: number;
  rowHeights: number[];
  height: number;
  width: number;
};

const palette: Palette = {
  background: "#f2f1ee",
  surface: "#ffffff",
  soft: "#e9e8e4",
  media: "#171717",
  border: "#8a8984",
  ink: "#181817",
  muted: "#5e5d59",
};

function metricsFor(size: GridExportSize, images: Map<string, LoadedImage>): Metrics {
  const naturalWidth = Math.max(1, ...Array.from(images.values(), image => image.width));
  const naturalHeight = Math.max(1, ...Array.from(images.values(), image => image.height));
  if (size === "full") {
    return {
      margin: 48,
      gap: 48,
      padding: 24,
      cellWidth: naturalWidth,
      cellHeight: naturalHeight,
      labelWidth: 560,
      titleFont: "700 44px Arial, sans-serif",
      subtitleFont: "24px Arial, sans-serif",
      sliceFont: "700 30px Arial, sans-serif",
      labelFont: "700 28px Arial, sans-serif",
      detailFont: "22px Arial, sans-serif",
      lineHeight: 36,
      detailLineHeight: 30,
    };
  }
  return {
    margin: 24,
    gap: 24,
    padding: 12,
    cellWidth: 256,
    cellHeight: 256,
    labelWidth: 300,
    titleFont: "700 28px Arial, sans-serif",
    subtitleFont: "16px Arial, sans-serif",
    sliceFont: "700 19px Arial, sans-serif",
    labelFont: "700 16px Arial, sans-serif",
    detailFont: "13px Arial, sans-serif",
    lineHeight: 22,
    detailLineHeight: 18,
  };
}

function breakLongToken(context: CanvasRenderingContext2D, token: string, maxWidth: number) {
  const chunks: string[] = [];
  let current = "";
  for (const character of token) {
    const candidate = current + character;
    if (current && context.measureText(candidate).width > maxWidth) {
      chunks.push(current);
      current = character;
    } else {
      current = candidate;
    }
  }
  if (current) chunks.push(current);
  return chunks;
}

function wrapText(context: CanvasRenderingContext2D, text: string, maxWidth: number) {
  const lines: string[] = [];
  for (const paragraph of (text || " ").split("\n")) {
    const words = paragraph.trim().split(/\s+/).filter(Boolean);
    if (!words.length) {
      lines.push("");
      continue;
    }
    let line = "";
    for (const word of words) {
      const parts = context.measureText(word).width > maxWidth ? breakLongToken(context, word, maxWidth) : [word];
      for (const part of parts) {
        const candidate = line ? `${line} ${part}` : part;
        if (line && context.measureText(candidate).width > maxWidth) {
          lines.push(line);
          line = part;
        } else {
          line = candidate;
        }
      }
    }
    if (line) lines.push(line);
  }
  return lines.length ? lines : [""];
}

function entryHeight(context: CanvasRenderingContext2D, entry: GridExportEntry, width: number, metrics: Metrics) {
  const textWidth = Math.max(1, width - metrics.padding * 2);
  context.font = metrics.labelFont;
  const labelHeight = wrapText(context, entry.label, textWidth).length * metrics.lineHeight;
  context.font = metrics.detailFont;
  const detailHeight = entry.detail ? wrapText(context, entry.detail, textWidth).length * metrics.detailLineHeight + metrics.padding / 2 : 0;
  return metrics.padding * 2 + labelHeight + detailHeight;
}

function drawEntry(
  context: CanvasRenderingContext2D,
  entry: GridExportEntry,
  x: number,
  y: number,
  width: number,
  height: number,
  metrics: Metrics,
  fill: string,
) {
  context.fillStyle = fill;
  context.fillRect(x, y, width, height);
  context.strokeStyle = palette.border;
  context.strokeRect(x + 0.5, y + 0.5, width - 1, height - 1);
  const textWidth = Math.max(1, width - metrics.padding * 2);
  let cursor = y + metrics.padding;
  context.textBaseline = "top";
  context.fillStyle = palette.ink;
  context.font = metrics.labelFont;
  for (const line of wrapText(context, entry.label, textWidth)) {
    context.fillText(line, x + metrics.padding, cursor);
    cursor += metrics.lineHeight;
  }
  if (entry.detail) {
    cursor += metrics.padding / 2;
    context.fillStyle = palette.muted;
    context.font = metrics.detailFont;
    for (const line of wrapText(context, entry.detail, textWidth)) {
      context.fillText(line, x + metrics.padding, cursor);
      cursor += metrics.detailLineHeight;
    }
  }
}

function drawContainedImage(
  context: CanvasRenderingContext2D,
  image: LoadedImage,
  x: number,
  y: number,
  width: number,
  height: number,
) {
  const scale = Math.min(width / image.width, height / image.height);
  const drawWidth = image.width * scale;
  const drawHeight = image.height * scale;
  context.drawImage(image.source, x + (width - drawWidth) / 2, y + (height - drawHeight) / 2, drawWidth, drawHeight);
}

async function loadImage(url: string): Promise<LoadedImage> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Image delivery failed with HTTP ${response.status}`);
  const blob = await response.blob();
  if (typeof createImageBitmap === "function") {
    const bitmap = await createImageBitmap(blob);
    return { source: bitmap, width: bitmap.width, height: bitmap.height, close: () => bitmap.close() };
  }
  const objectUrl = URL.createObjectURL(blob);
  const image = new Image();
  image.src = objectUrl;
  await image.decode();
  return {
    source: image,
    width: image.naturalWidth,
    height: image.naturalHeight,
    close: () => URL.revokeObjectURL(objectUrl),
  };
}

async function loadImages(options: ExportOptions) {
  const assetIds = Array.from(new Set(options.slices.flatMap(slice => slice.cells.flatMap(row => row.map(cell => cell.assetRevisionId).filter((value): value is string => Boolean(value))))));
  const images = new Map<string, LoadedImage>();
  const failures = new Set<string>();
  const variant: AssetVariant = options.size === "full" ? "content" : "thumbnail";
  const maxPixels = options.size === "full" ? 1024 : 512;
  let cursor = 0;
  const workers = Array.from({ length: Math.min(4, assetIds.length) }, async () => {
    while (cursor < assetIds.length) {
      const assetId = assetIds[cursor++];
      try {
        const url = await options.resolveImage(assetId, variant, maxPixels);
        images.set(assetId, await loadImage(url));
      } catch {
        failures.add(assetId);
      }
    }
  });
  await Promise.all(workers);
  return { images, failures };
}

function layoutSlices(context: CanvasRenderingContext2D, slices: GridExportSlice[], metrics: Metrics) {
  return slices.map((slice): SliceLayout => {
    const corner: GridExportEntry = { label: slice.cornerLabel };
    const width = metrics.labelWidth + slice.columns.length * metrics.cellWidth;
    context.font = metrics.sliceFont;
    const titleLines = slice.title ? wrapText(context, slice.title, width) : [];
    const titleHeight = titleLines.length ? titleLines.length * metrics.lineHeight + metrics.padding : 0;
    const headerHeight = Math.max(entryHeight(context, corner, metrics.labelWidth, metrics), ...slice.columns.map(entry => entryHeight(context, entry, metrics.cellWidth, metrics)));
    const rowHeights = slice.rows.map(entry => Math.max(metrics.cellHeight, entryHeight(context, entry, metrics.labelWidth, metrics)));
    const height = titleHeight + headerHeight + rowHeights.reduce((total, rowHeight) => total + rowHeight, 0);
    return { slice, titleHeight, titleLines, headerHeight, rowHeights, width, height };
  });
}

function canvasBlob(canvas: HTMLCanvasElement) {
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error("The browser could not encode the grid image.")), "image/png");
  });
}

export async function renderGridImage(options: ExportOptions) {
  if (!options.slices.length) throw new Error("This grid has no slices to export.");
  const { images, failures } = await loadImages(options);
  try {
    const measureCanvas = document.createElement("canvas");
    const measureContext = measureCanvas.getContext("2d");
    if (!measureContext) throw new Error("Canvas rendering is unavailable in this browser.");
    const metrics = metricsFor(options.size, images);
    const layouts = layoutSlices(measureContext, options.slices, metrics);
    const width = Math.ceil(Math.max(...layouts.map(layout => layout.width)) + metrics.margin * 2);
    const contentWidth = width - metrics.margin * 2;
    const titleLineHeight = options.size === "full" ? 56 : 36;
    const subtitleLineHeight = options.size === "full" ? 32 : 22;
    measureContext.font = metrics.titleFont;
    const titleLines = wrapText(measureContext, options.title, contentWidth);
    measureContext.font = metrics.subtitleFont;
    const subtitleLines = wrapText(measureContext, options.subtitle, contentWidth);
    const titleHeight = metrics.margin + titleLines.length * titleLineHeight + metrics.padding + subtitleLines.length * subtitleLineHeight + metrics.margin;
    const height = Math.ceil(titleHeight + layouts.reduce((total, layout) => total + layout.height, 0) + metrics.gap * Math.max(0, layouts.length - 1) + metrics.margin);
    if (width > 32767 || height > 32767 || width * height > 268_000_000) {
      throw new Error(`The ${options.size} export would be ${width} × ${height}px, which exceeds the browser canvas limit. Export a smaller grid or use the compact option.`);
    }
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Canvas rendering is unavailable in this browser.");
    context.fillStyle = palette.background;
    context.fillRect(0, 0, width, height);
    context.textBaseline = "top";
    let titleTop = metrics.margin;
    context.fillStyle = palette.ink;
    context.font = metrics.titleFont;
    for (const line of titleLines) {
      context.fillText(line, metrics.margin, titleTop);
      titleTop += titleLineHeight;
    }
    titleTop += metrics.padding;
    context.fillStyle = palette.muted;
    context.font = metrics.subtitleFont;
    for (const line of subtitleLines) {
      context.fillText(line, metrics.margin, titleTop);
      titleTop += subtitleLineHeight;
    }
    let top = titleHeight;
    for (const layout of layouts) {
      const { slice } = layout;
      if (layout.titleLines.length) {
        context.fillStyle = palette.ink;
        context.font = metrics.sliceFont;
        layout.titleLines.forEach((line, index) => context.fillText(line, metrics.margin, top + index * metrics.lineHeight));
        top += layout.titleHeight;
      }
      const left = metrics.margin;
      drawEntry(context, { label: slice.cornerLabel }, left, top, metrics.labelWidth, layout.headerHeight, metrics, palette.soft);
      slice.columns.forEach((entry, columnIndex) => {
        drawEntry(context, entry, left + metrics.labelWidth + columnIndex * metrics.cellWidth, top, metrics.cellWidth, layout.headerHeight, metrics, palette.soft);
      });
      top += layout.headerHeight;
      slice.rows.forEach((entry, rowIndex) => {
        const rowHeight = layout.rowHeights[rowIndex];
        drawEntry(context, entry, left, top, metrics.labelWidth, rowHeight, metrics, palette.soft);
        slice.columns.forEach((_column, columnIndex) => {
          const cell = slice.cells[rowIndex]?.[columnIndex] ?? {};
          const cellX = left + metrics.labelWidth + columnIndex * metrics.cellWidth;
          context.fillStyle = palette.media;
          context.fillRect(cellX, top, metrics.cellWidth, rowHeight);
          context.strokeStyle = palette.border;
          context.strokeRect(cellX + 0.5, top + 0.5, metrics.cellWidth - 1, rowHeight - 1);
          const image = cell.assetRevisionId ? images.get(cell.assetRevisionId) : undefined;
          if (image) {
            drawContainedImage(context, image, cellX, top, metrics.cellWidth, rowHeight);
          } else {
            context.fillStyle = "#d8d7d2";
            context.font = metrics.detailFont;
            context.textAlign = "center";
            const message = cell.assetRevisionId && failures.has(cell.assetRevisionId) ? "Image unavailable" : cell.status || "No image";
            context.fillText(message, cellX + metrics.cellWidth / 2, top + rowHeight / 2 - metrics.detailLineHeight / 2);
            context.textAlign = "left";
          }
        });
        top += rowHeight;
      });
      top += metrics.gap;
    }
    return canvasBlob(canvas);
  } finally {
    images.forEach(image => image.close());
  }
}

export async function downloadGridImage(options: ExportOptions) {
  const blob = await renderGridImage(options);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = options.filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
