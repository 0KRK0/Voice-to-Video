/**
 * Defaults for new projects.
 *
 * Kept in this browser, and the Settings screen says so. That is not a
 * placeholder for a server feature — it is the honest shape of what exists:
 * `POST /v1/projects` takes a style and an aspect ratio, `POST .../pacing` takes
 * a mode, and there is no account-level preferences store anywhere in the API.
 *
 * The alternative was a Defaults tab that looked like it saved to the account
 * and silently did not, which is the kind of thing that is only discovered when
 * a colleague's projects come out in the wrong aspect ratio.
 */

import type { ExecutionTarget, PacingMode } from "../api/types.js";

export interface ProjectDefaults {
  style: string;
  language: string;
  pacing: PacingMode;
  aspectRatio: string;
}

const KEY = "vtv.defaults";

export const STYLES = [
  "documentary",
  "explainer",
  "corporate",
  "cinematic",
  "editorial",
] as const;

export const ASPECTS = ["16:9", "9:16", "1:1", "4:5"] as const;

export const PACINGS: PacingMode[] = [
  "natural",
  "tight",
  "cinematic",
  "educational",
  "fast",
];

const FALLBACK: ProjectDefaults = {
  style: "documentary",
  language: "en",
  pacing: "natural",
  aspectRatio: "16:9",
};

export function readDefaults(): ProjectDefaults {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...FALLBACK };
    const parsed = JSON.parse(raw) as Partial<ProjectDefaults>;
    return {
      style: typeof parsed.style === "string" ? parsed.style : FALLBACK.style,
      language:
        typeof parsed.language === "string" ? parsed.language : FALLBACK.language,
      pacing: (PACINGS as string[]).includes(String(parsed.pacing))
        ? (parsed.pacing as PacingMode)
        : FALLBACK.pacing,
      aspectRatio:
        typeof parsed.aspectRatio === "string"
          ? parsed.aspectRatio
          : FALLBACK.aspectRatio,
    };
  } catch {
    return { ...FALLBACK };
  }
}

export function writeDefaults(next: ProjectDefaults): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // A browser with storage disabled gets the fallbacks every time, which is
    // a worse experience and not a broken one.
  }
}


/**
 * Where renders should run, chosen on the Computers screen.
 *
 * Kept beside the project defaults and for the same reason — there is no
 * account-level preferences store in the API — but with a smaller consequence
 * when it is wrong. The server resolves `auto` itself and refuses `device`
 * outright when no computer is available, so a stale value here produces a
 * clear refusal rather than a render in a place nobody chose.
 */
const EXECUTION_KEY = "vtv.execution";

export function readExecution(): ExecutionTarget {
  try {
    const raw = localStorage.getItem(EXECUTION_KEY);
    return raw === "device" || raw === "cloud" ? raw : "auto";
  } catch {
    return "auto";
  }
}

export function writeExecution(next: ExecutionTarget): void {
  try {
    localStorage.setItem(EXECUTION_KEY, next);
  } catch {
    // Storage disabled. Every render is then "automatically", which is the
    // right thing to fall back to and not a broken state.
  }
}
