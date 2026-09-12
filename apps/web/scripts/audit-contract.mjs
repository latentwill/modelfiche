import { readdir, readFile } from "node:fs/promises";

const apiUrl = process.env.TITLES_API_URL ?? "http://127.0.0.1:8400";
const sourceRoot = new URL("../src/", import.meta.url);
const files = (await readdir(sourceRoot)).filter(file => /\.(ts|tsx)$/.test(file) && !/\.(test|spec)\./.test(file));
const source = (await Promise.all(files.map(file => readFile(new URL(file, sourceRoot), "utf8")))).join("\n");
const scannable = source
  .replace(/\$\{query\(\{[^}]*\}\)\}/g, "")
  .replace(/\$\{[^}]+\}/g, "{param}");
const referenced = [...scannable.matchAll(/\/api\/[A-Za-z0-9_?&=/{},.:-]+/g)].map(match => match[0].split("?")[0]);
const openapi = await fetch(`${apiUrl}/openapi.json`).then(response => {
  if (!response.ok) throw new Error(`OpenAPI request failed: ${response.status}`);
  return response.json();
});
// Remote-access session exchange is served by security middleware, before the
// FastAPI router, so it intentionally does not appear in OpenAPI.
const middlewarePaths = ["/api/remote-session"];
const available = [...Object.keys(openapi.paths), ...middlewarePaths];
const unmatched = [...new Set(referenced)].filter(path => !available.some(candidate => sameShape(path, candidate)));
const referencedAvailable = available.filter(candidate => referenced.some(path => sameShape(path, candidate)));
const report = {
  api_url: apiUrl,
  source_files: files.length,
  referenced_path_shapes: new Set(referenced.map(normalize)).size,
  available_paths: available.length,
  middleware_paths: middlewarePaths,
  referenced_available_paths: referencedAvailable.length,
  unmatched_frontend_paths: unmatched,
  backend_only_paths: available.filter(path => !referencedAvailable.includes(path)),
};
console.log(JSON.stringify(report, null, 2));
if (unmatched.length) process.exitCode = 1;

function normalize(path) {
  return path.replace(/\$\{[^}]+\}/g, "{param}").replace(/\{[^}]+\}/g, "{param}").replace(/\/$/, "");
}

function sameShape(left, right) {
  const a = normalize(left).split("/");
  const b = normalize(right).split("/");
  return a.length === b.length && a.every((part, index) => part === b[index] || part.includes("{param}") || b[index].includes("{param}"));
}
