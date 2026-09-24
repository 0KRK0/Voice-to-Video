/**
 * Formatting, in one place.
 *
 * Three of these are load-bearing rather than cosmetic:
 *
 * `timecode` is the product's clock. It appears in the toolbar, the ruler, the
 * inspector and the clip tooltips, and those four must agree to the
 * millisecond or a user reporting "the cut is at 0:22.4" is reporting
 * something nobody can find.
 *
 * `humaniseEnum` turns `use_generated_image` into "Use a generated image". The
 * backend's enums are deliberately closed and machine-shaped; showing them raw
 * leaks an implementation detail into the interface, and letting each component
 * humanise its own would produce four spellings of the same word.
 *
 * `bytes` and `money` exist so that a size and a cost look the same everywhere
 * they appear.
 */
/** `0:22.400` — the form used on the ruler and in every readout. */
export function timecode(seconds, showMillis = true) {
    if (!Number.isFinite(seconds) || seconds < 0)
        seconds = 0;
    // Round to milliseconds *first*, then split.
    //
    // Splitting first and rounding the remainder is the obvious way and it is
    // wrong at every second boundary: 0.9999 floors to 0 whole seconds with a
    // remainder that rounds to 1000, and the clock reads `0:00.1000` — a
    // four-digit millisecond field, once per second, in the ruler, the toolbar,
    // the inspector and every tooltip. Rounding once carries the second properly.
    const total = Math.round(seconds * 1000);
    const whole = Math.floor(total / 1000);
    const millis = total % 1000;
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    const secs = whole % 60;
    const head = hours > 0
        ? `${hours}:${String(minutes).padStart(2, "0")}`
        : String(minutes);
    const body = `${head}:${String(secs).padStart(2, "0")}`;
    return showMillis ? `${body}.${String(millis).padStart(3, "0")}` : body;
}
/** `6:58` — the coarse form used in lists and durations. */
export function clock(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
        return "—";
    }
    return timecode(seconds, false);
}
/** `+4.2s` / `−11.0s`. Signed, because the sign is the information. */
export function delta(seconds) {
    if (Math.abs(seconds) < 0.05)
        return "no change";
    // A real minus sign, not a hyphen: at 11px a hyphen next to a digit reads as
    // a dash in a range.
    const sign = seconds > 0 ? "+" : "−";
    return `${sign}${Math.abs(seconds).toFixed(1)}s`;
}
export function bytes(value) {
    if (value === null || value === undefined || !Number.isFinite(value)) {
        return "—";
    }
    if (value < 1024)
        return `${value} B`;
    const units = ["KB", "MB", "GB"];
    let size = value / 1024;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
        size /= 1024;
        unit += 1;
    }
    return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
}
/**
 * A cost the customer sees.
 *
 * Four decimal places, because a single visual costs fractions of a cent and
 * rounding it to `$0.00` would make the whole per-version cost display
 * meaningless.
 */
export function money(usd) {
    if (usd === null || usd === undefined || !Number.isFinite(usd))
        return "—";
    if (usd === 0)
        return "free";
    if (usd < 0.01)
        return `$${usd.toFixed(4)}`;
    return `$${usd.toFixed(2)}`;
}
/** `2 minutes ago`, `Yesterday`, `Last week`. */
export function since(iso) {
    if (!iso)
        return "—";
    const then = Date.parse(iso);
    if (Number.isNaN(then))
        return "—";
    const seconds = Math.max(0, (Date.now() - then) / 1000);
    if (seconds < 45)
        return "just now";
    if (seconds < 90)
        return "a minute ago";
    if (seconds < 3600)
        return `${Math.round(seconds / 60)} minutes ago`;
    if (seconds < 7200)
        return "an hour ago";
    if (seconds < 86_400)
        return `${Math.round(seconds / 3600)} hours ago`;
    if (seconds < 172_800)
        return "yesterday";
    if (seconds < 604_800)
        return `${Math.round(seconds / 86_400)} days ago`;
    if (seconds < 1_209_600)
        return "last week";
    return new Date(then).toLocaleDateString(undefined, {
        day: "numeric",
        month: "short",
    });
}
/** `14:44` — the wall clock, for render timestamps. */
export function timeOfDay(iso) {
    if (!iso)
        return "—";
    const at = Date.parse(iso);
    if (Number.isNaN(at))
        return "—";
    return new Date(at).toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
    });
}
/**
 * Words the interface uses for the backend's machine names.
 *
 * An explicit table rather than a `replace(/_/g, " ")`, because several of
 * these need more than de-snaking: `use_real_source` is "Use a real source",
 * not "Use real source", and `sfx` is "SFX" rather than "Sfx".
 */
const WORDS = {
    // Regeneration intents
    same_idea: "Same idea, another attempt",
    more_cinematic: "More cinematic",
    more_realistic: "More realistic",
    more_educational: "More educational",
    simpler: "Simpler",
    use_real_source: "Use a real source",
    use_animation: "Use animation",
    use_generated_image: "Generate an image",
    use_generated_video: "Generate a video",
    use_typography: "Use typography",
    // Revision kinds
    fix_grammar: "Fix grammar",
    improve_clarity: "Improve clarity",
    enhance: "Enhance",
    shorten: "Shorten",
    expand: "Expand",
    make_formal: "Make formal",
    make_cinematic: "Make cinematic",
    make_educational: "Make educational",
    make_concise: "Make concise",
    translate: "Translate",
    // Strategies
    existing_asset: "Your media",
    programmatic: "Drawn by the system",
    licensed_media: "Licensed source",
    generated_image: "Generated image",
    generated_video: "Generated video",
    typography: "Typography",
    stock_footage: "Stock footage",
    // Track kinds
    narration: "Narration",
    visual: "Visual",
    caption: "Captions",
    music: "Music",
    sfx: "SFX",
    overlay: "Overlay",
    broll: "B-roll",
    graphics: "Graphics",
    secondary_voice: "Second voice",
    chapter: "Chapters",
    // Pacing
    natural: "Natural",
    tight: "Tight",
    cinematic: "Cinematic",
    educational: "Educational",
    fast: "Fast",
    // Media
    user_upload: "Your media",
    licensed_source: "Licensed source",
    ai_generated: "AI generated",
    image: "Image",
    video: "Video",
    audio: "Audio",
    logo: "Logo",
    vector: "SVG",
};
export function humanise(value) {
    if (!value)
        return "";
    const known = WORDS[value];
    if (known)
        return known;
    const spaced = value.replace(/_/g, " ");
    return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
/** `3 visuals` / `1 visual`. */
export function plural(count, one, many = `${one}s`) {
    return `${count} ${count === 1 ? one : many}`;
}
/** Clamp, used everywhere a pointer position becomes a time. */
export function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
}
/** Round to the backend's millisecond precision, so client and server agree. */
export function quantise(seconds) {
    return Math.round(seconds * 1000) / 1000;
}
//# sourceMappingURL=format.js.map