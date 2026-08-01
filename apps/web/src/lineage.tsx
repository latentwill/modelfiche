import { activeWorkspaceId, listOf, str } from "./api";

type Row = Record<string, unknown>;

function lineageHref(value: string, projectId: string) {
  if (!value.startsWith("#/")) return value;
  const [path, rawQuery = ""] = value.split("?", 2);
  const params = new URLSearchParams(rawQuery);
  if (projectId && !params.has("project") && !path.startsWith("#/project/")) params.set("project", projectId);
  const workspaceId = activeWorkspaceId();
  if (workspaceId && !params.has("workspace")) params.set("workspace", workspaceId);
  const query = params.toString();
  return `${path}${query ? `?${query}` : ""}`;
}

function entity(row: Row, side: "source" | "target") {
  const label = str(row[`${side}_label`] ?? row[`${side}_id`], "Unknown object");
  const href = str(row[`${side}_href`], "");
  const projectId = str(row.project_id, "");
  const type = str(row[`${side}_type`], "object").replaceAll("_", " ");
  return <span className="lineage-entity"><small>{type}</small>{href ? <a href={lineageHref(href, projectId)}>{label}</a> : <strong>{label}</strong>}</span>;
}

function metadataEntries(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value as Row).filter(([, item]) => item !== null && item !== undefined && item !== "");
}

export function LineageList({ value }: { value: unknown }) {
  const edges = listOf<Row>(value);
  return <ol className="lineage-list" aria-label="Typed lineage relationships">{edges.map((edge, index) => {
    const metadata = metadataEntries(edge.metadata);
    return <li key={str(edge.id, String(index))}>
      <div className="lineage-route">{entity(edge, "source")}<span className="lineage-relationship">{str(edge.relationship, "related to").replaceAll("_", " ")}</span>{entity(edge, "target")}</div>
      {!!metadata.length && <dl>{metadata.map(([key, item]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{typeof item === "object" ? JSON.stringify(item) : String(item)}</dd></div>)}</dl>}
      {Boolean(edge.created_at) && <time dateTime={str(edge.created_at)}>{new Date(str(edge.created_at)).toLocaleString()}</time>}
    </li>;
  })}</ol>;
}
