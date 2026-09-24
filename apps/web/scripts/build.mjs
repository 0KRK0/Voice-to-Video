/**
 * The production build. Zero dependencies, about a hundred lines.
 *
 * `tsc` has already emitted ES modules into `dist/js`. This script does the
 * four things a bundler would otherwise be imported for:
 *
 * 1. copies the stylesheets and the HTML shell into `dist`
 * 2. fingerprints every asset by content hash, so they can be cached forever
 * 3. rewrites the references in the HTML and inside the emitted modules
 * 4. reports the total, so a regression in size is visible rather than gradual
 *
 * ## Why no bundling
 *
 * The output is ~40 native ES modules. Over HTTP/2 that is a handful of
 * multiplexed requests against a cache that survives a partial deploy, and it
 * costs nothing at this size. Concatenating them would buy a few milliseconds
 * on first load and cost the ability to ship a one-line fix without
 * invalidating the whole application.
 *
 * If that trade ever stops making sense, this is the file to change, and
 * nothing else in the application will notice.
 */

import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const dist = join(root, "dist");
const jsOut = join(dist, "js");
const cssOut = join(dist, "css");

function walk(directory) {
  const found = [];
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) found.push(...walk(path));
    else found.push(path);
  }
  return found;
}

function hash(contents) {
  return createHash("sha256").update(contents).digest("hex").slice(0, 10);
}

if (!existsSync(jsOut)) {
  console.error(
    "dist/js is missing — run `tsc` first (npm run build does both).",
  );
  process.exit(1);
}

// -- stylesheets ------------------------------------------------------------

rmSync(cssOut, { recursive: true, force: true });
mkdirSync(cssOut, { recursive: true });
cpSync(join(root, "src", "design"), cssOut, { recursive: true });

if (existsSync(join(root, "public"))) {
  cpSync(join(root, "public"), dist, { recursive: true });
}

// -- fingerprint ------------------------------------------------------------

/**
 * Map every emitted file to a content-addressed name.
 *
 * Source maps keep their plain names: they are only fetched when a developer
 * opens the tools, and a hashed map is one more rewrite for no benefit.
 */
const renames = new Map();

for (const path of [...walk(jsOut), ...walk(cssOut)]) {
  if (path.endsWith(".map")) continue;
  const contents = readFileSync(path);
  const digest = hash(contents);
  const web = "/" + relative(dist, path).split(/[\\/]/).join("/");
  const hashed = web.replace(/\.(js|css)$/, `.${digest}.$1`);
  renames.set(web, hashed);
}

/**
 * Rewrite every reference, then move the files.
 *
 * Two passes, because a module that imports another must be rewritten before
 * its own hash is computed — otherwise the hash describes content that is
 * about to change. This is why the map above is built from the *unrewritten*
 * files and the final names are recomputed below.
 */
/** Every place a module names another file. */
// `[^"'\n]` — a module specifier is a single string literal and can never
// contain a newline. Allowing one made the scanner read across a line break:
// a sentence ending `…downloaded from "` followed by `+ "Export will play…"`
// matched as `from` + quote + newline-and-plus + quote, and the build failed
// claiming a bare import of a string of whitespace. That is the second
// false-positive of this kind (the first was prose inside a comment, fixed by
// `commentRanges`), and both were fixed in the scanner rather than by
// rewording the English — a scanner that misreads prose will one day misread
// prose that happens to look like a real path, and rewrite it.
const SPECIFIER =
  /(\bfrom\s*|\bimport\s*\(?\s*|\bexport\s+\*\s+from\s*)(["'])([^"'\n]+)\2/g;

/**
 * Rewrite one file's references to their fingerprinted names.
 *
 * Specifiers are **resolved** against the file's own directory rather than
 * matched as strings. The first version of this compared text, and missed
 * `"../core/dom.js"` because it had constructed the candidate as
 * `"./../core/dom.js"` — the same path, spelled differently. Every module two
 * levels deep silently kept an unhashed import, the server answered those with
 * the SPA fallback, and the browser refused nineteen `text/html` modules.
 *
 * Resolving makes spelling irrelevant, which is the only way this can be right
 * for specifiers nobody has thought of yet.
 */
/**
 * The byte ranges of every comment in a module.
 *
 * Needed because the specifier pattern is a regex over the whole file, and a
 * comment is prose: the sentence *"Fell back" is a different fact from
 * "failed"* matches `\bfrom\s*"..."` exactly as well as a real import does,
 * and the build died claiming `failed` was a runtime dependency.
 *
 * Rewording the comment would have fixed that comment. This fixes the class —
 * and the class matters, because the failure mode is not always a loud one: a
 * comment containing `from "./thing.js"` would have been silently *rewritten*.
 *
 * A small state machine rather than a parser. It tracks strings and template
 * literals as well as comments, so a `"// not a comment"` string does not open
 * one. It does not track regex literals, which cannot contain an unescaped
 * quote followed by a specifier-shaped body without also being a syntax error
 * in practice.
 */
function commentRanges(text) {
  const ranges = [];
  let index = 0;
  let state = "code";
  let start = 0;

  while (index < text.length) {
    const two = text.slice(index, index + 2);
    const char = text[index];

    if (state === "code") {
      if (two === "//") {
        state = "line";
        start = index;
        index += 2;
        continue;
      }
      if (two === "/*") {
        state = "block";
        start = index;
        index += 2;
        continue;
      }
      if (char === '"' || char === "'" || char === "`") {
        state = char;
        index += 1;
        continue;
      }
      index += 1;
      continue;
    }

    if (state === "line") {
      if (char === "\n") {
        ranges.push([start, index]);
        state = "code";
      }
      index += 1;
      continue;
    }

    if (state === "block") {
      if (two === "*/") {
        ranges.push([start, index + 2]);
        state = "code";
        index += 2;
        continue;
      }
      index += 1;
      continue;
    }

    // Inside a string or template literal.
    if (char === "\\") {
      index += 2;
      continue;
    }
    if (char === state) state = "code";
    index += 1;
  }

  if (state === "line" || state === "block") ranges.push([start, text.length]);
  return ranges;
}

function rewrite(text, fromPath) {
  const fromDir = dirname(fromPath);
  const comments = commentRanges(text);
  const inComment = (at) =>
    comments.some(([from, to]) => at >= from && at < to);

  return text.replace(SPECIFIER, (whole, lead, quote, specifier, offset) => {
    // Prose is not an import.
    if (inComment(offset)) return whole;
    // Absolute web paths, as the HTML and the entry module use.
    if (specifier.startsWith("/")) {
      const hashed = renames.get(specifier);
      return hashed ? `${lead}${quote}${hashed}${quote}` : whole;
    }
    // Relative specifiers, as every emitted module uses.
    if (specifier.startsWith(".")) {
      const target = resolve(fromDir, specifier);
      const web = "/" + relative(dist, target).split(/[\\/]/).join("/");
      const hashed = renames.get(web);
      return hashed ? `${lead}${quote}${hashed}${quote}` : whole;
    }
    // A bare specifier would be a runtime dependency. There are none, and one
    // appearing is worth failing the build over rather than shipping broken.
    throw new Error(
      `bare import "${specifier}" in ${relative(root, fromPath)} — this app has no runtime dependencies`,
    );
  });
}

/** The HTML references assets by absolute path in attributes, not imports. */
function rewriteHtml(text, names) {
  let out = text;
  for (const [web, hashed] of names) {
    out = out.split(`"${web}"`).join(`"${hashed}"`);
  }
  return out;
}

const finalNames = new Map();
let total = 0;

for (const [web] of renames) {
  const source = join(dist, web.slice(1));
  const rewritten = rewrite(readFileSync(source, "utf8"), source);
  const digest = hash(rewritten);
  const hashed = web.replace(/\.(js|css)$/, `.${digest}.$1`);
  finalNames.set(web, hashed);
  writeFileSync(join(dist, hashed.slice(1)), rewritten);
  rmSync(source);
  total += Buffer.byteLength(rewritten);
}

// The map used for rewriting was provisional; redo the pass with final names
// so imports point at files that exist.
for (const [, hashed] of finalNames) {
  const path = join(dist, hashed.slice(1));
  let text = readFileSync(path, "utf8");
  for (const [web, final] of finalNames) {
    const provisional = renames.get(web);
    if (provisional && provisional !== final) {
      text = text.split(provisional).join(final);
    }
  }
  writeFileSync(path, text);
}

// -- sweep ------------------------------------------------------------------

/**
 * Delete every asset this build did not produce.
 *
 * Content-addressed names never collide, which is the point of them and also
 * the trap: a rebuild after an edit writes a *new* name and leaves the old file
 * sitting there. Nothing references it, so nothing breaks — it simply ships,
 * forever, and the size report counts it. Two builds in this repository turned
 * 33 assets into 61 and 302 KB into 549 KB, and the second number was reported
 * as the application's size with a straight face.
 *
 * The script that writes the directory is the only thing that knows what
 * belongs in it, so it is the only thing that can say what does not.
 */
const kept = new Set(
  [...finalNames.values()].map((web) => join(dist, web.slice(1))),
);
let swept = 0;
for (const path of [...walk(jsOut), ...walk(cssOut)]) {
  if (path.endsWith(".map")) continue;
  if (kept.has(path)) continue;
  rmSync(path);
  swept += 1;
}

// -- html -------------------------------------------------------------------

const html = rewriteHtml(readFileSync(join(root, "index.html"), "utf8"), finalNames);
writeFileSync(join(dist, "index.html"), html);

// -- report -----------------------------------------------------------------

const kb = (value) => `${(value / 1024).toFixed(1)} KB`;
console.log(`built ${finalNames.size} assets · ${kb(total)} total`);
if (swept) console.log(`  swept ${swept} stale asset${swept === 1 ? "" : "s"}`);
console.log(`  html  ${kb(Buffer.byteLength(html))}`);
console.log(`  js    ${kb(sizeOf(jsOut))}`);
console.log(`  css   ${kb(sizeOf(cssOut))}`);
console.log("  runtime dependencies: none");

function sizeOf(directory) {
  if (!existsSync(directory)) return 0;
  return walk(directory)
    .filter((path) => !path.endsWith(".map"))
    .reduce((sum, path) => sum + statSync(path).size, 0);
}
