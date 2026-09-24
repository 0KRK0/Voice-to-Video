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
const KEY = "vtv.defaults";
export const STYLES = [
    "documentary",
    "explainer",
    "corporate",
    "cinematic",
    "editorial",
];
export const ASPECTS = ["16:9", "9:16", "1:1", "4:5"];
export const PACINGS = [
    "natural",
    "tight",
    "cinematic",
    "educational",
    "fast",
];
const FALLBACK = {
    style: "documentary",
    language: "en",
    pacing: "natural",
    aspectRatio: "16:9",
};
export function readDefaults() {
    try {
        const raw = localStorage.getItem(KEY);
        if (!raw)
            return { ...FALLBACK };
        const parsed = JSON.parse(raw);
        return {
            style: typeof parsed.style === "string" ? parsed.style : FALLBACK.style,
            language: typeof parsed.language === "string" ? parsed.language : FALLBACK.language,
            pacing: PACINGS.includes(String(parsed.pacing))
                ? parsed.pacing
                : FALLBACK.pacing,
            aspectRatio: typeof parsed.aspectRatio === "string"
                ? parsed.aspectRatio
                : FALLBACK.aspectRatio,
        };
    }
    catch {
        return { ...FALLBACK };
    }
}
export function writeDefaults(next) {
    try {
        localStorage.setItem(KEY, JSON.stringify(next));
    }
    catch {
        // A browser with storage disabled gets the fallbacks every time, which is
        // a worse experience and not a broken one.
    }
}
//# sourceMappingURL=defaults.js.map