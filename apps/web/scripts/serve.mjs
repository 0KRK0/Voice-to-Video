/**
 * A static server for the built app, with SPA fallback.
 *
 * Development and testing only — production serves `dist/` from whatever is in
 * front of the API. It exists because the router uses real paths, so
 * `/studio/prj_…` has to return `index.html` rather than 404, and no amount of
 * `file://` will do that.
 *
 * Two behaviours worth naming: fingerprinted assets are served immutable (they
 * are content-addressed, so they can be), and `index.html` is served
 * `no-store` (it is the thing that points at the fingerprints).
 */

import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";

const dist = resolve(process.argv[2] ?? "dist");
const port = Number(process.argv[3] ?? 5173);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".webm": "video/webm",
  ".mp4": "video/mp4",
};

const FINGERPRINTED = /\.[0-9a-f]{10}\.(js|css)$/;

createServer((request, response) => {
  const url = new URL(request.url ?? "/", "http://localhost");
  // `normalize` plus the prefix check is the whole traversal defence: a path
  // that escapes the root after normalising does not match, and is refused.
  const wanted = join(dist, normalize(decodeURIComponent(url.pathname)));

  const path =
    wanted.startsWith(dist) && existsSync(wanted) && statSync(wanted).isFile()
      ? wanted
      : join(dist, "index.html");

  if (!existsSync(path)) {
    response.writeHead(404, { "Content-Type": "text/plain" });
    response.end("not found");
    return;
  }

  const type = TYPES[extname(path)] ?? "application/octet-stream";
  response.writeHead(200, {
    "Content-Type": type,
    "Cache-Control": FINGERPRINTED.test(path)
      ? "public, max-age=31536000, immutable"
      : "no-store",
  });
  createReadStream(path).pipe(response);
}).listen(port, () => {
  console.log(`serving ${dist} on http://127.0.0.1:${port}`);
});
