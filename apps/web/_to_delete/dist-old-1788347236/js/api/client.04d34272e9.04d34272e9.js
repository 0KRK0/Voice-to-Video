/**
 * The one place this application talks to the server.
 *
 * Everything goes through `request()`: authentication, the idempotency header,
 * the error envelope, and the retry rule. Nothing else in the app calls
 * `fetch`, which is what makes those four things guarantees rather than
 * conventions.
 *
 * ## The error contract
 *
 * The backend answers every failure with `{error: {code, message, retryable}}`
 * and writes `message` for a human. This client turns that into an `ApiError`
 * carrying all three, and **never paraphrases the message**. Every refusal in
 * the system — a lock, an overlap, a derived track, a version conflict — comes
 * with a sentence naming its remedy, and rewording it client-side would throw
 * away the most useful part.
 *
 * ## No DOM
 *
 * This module reaches for `fetch`, `crypto.randomUUID` and `setTimeout` and
 * nothing else — no `window`, no `document`. That is deliberate rather than
 * incidental: it is what lets the transport's rules (the retry gate, the error
 * envelope, the idempotency key) be tested directly under Node instead of only
 * through a browser, and those are the rules most worth testing.
 *
 * ## Idempotency
 *
 * Every expensive call takes a client-generated key. The backend namespaces it
 * by tenant *and operation*, so reusing one key across two calls is safe; what
 * is not safe is *retrying* without reusing it, which is how a user is charged
 * twice for one regeneration. `postJob` makes reuse the default by generating
 * the key once and holding it for the life of the returned promise.
 */
/** A failure the server described. Carries the sentence meant for the user. */
export class ApiError extends Error {
    code;
    status;
    retryable;
    constructor(status, body) {
        super(body.message || "Something went wrong.");
        this.name = "ApiError";
        this.code = body.code || "internal_error";
        this.status = status;
        this.retryable = Boolean(body.retryable);
    }
    /**
     * A refused-but-well-formed edit, as opposed to a malformed request.
     *
     * Both arrive as `schema_invalid`; only the status separates them. The
     * editor needs the distinction because one means "your operation was not
     * allowed, here is why" and the other means "the client sent nonsense".
     */
    get isRefusal() {
        return this.status === 403;
    }
    /** A stale `expected_version` on a timeline edit. Offer reload, not retry. */
    get isConflict() {
        return this.status === 403 && /reload/i.test(this.message);
    }
}
/** Thrown when the network itself failed. Distinct from a server refusal. */
export class OfflineError extends Error {
    constructor() {
        super("We could not reach the server.");
        this.name = "OfflineError";
    }
}
/** Codes that are worth trying again without asking the user. */
const RETRYABLE_STATUS = new Set([502, 503, 504]);
function newKey() {
    return crypto.randomUUID();
}
export class VtvClient {
    baseUrl;
    token;
    constructor(options = {}) {
        this.baseUrl = (options.baseUrl ?? "").replace(/\/$/, "");
        this.token = options.token ?? null;
    }
    setToken(token) {
        this.token = token;
    }
    hasToken() {
        return Boolean(this.token);
    }
    // -- transport ----------------------------------------------------------
    async request(path, options = {}) {
        const { method = "GET", body, form, idempotencyKey, signal } = options;
        const headers = {};
        if (this.token)
            headers.Authorization = `Bearer ${this.token}`;
        if (idempotencyKey)
            headers["Idempotency-Key"] = idempotencyKey;
        if (body !== undefined)
            headers["Content-Type"] = "application/json";
        const init = { method, headers };
        if (signal)
            init.signal = signal;
        if (form)
            init.body = form;
        else if (body !== undefined)
            init.body = JSON.stringify(body);
        let response;
        try {
            response = await fetch(`${this.baseUrl}${path}`, init);
        }
        catch (cause) {
            if (signal?.aborted)
                throw cause;
            throw new OfflineError();
        }
        // One retry, only for the three statuses that mean "the server is having a
        // moment", and never for a call that may have already spent money. A retry
        // of a queued job is safe *because* of the idempotency key, but a retry of
        // one without a key is not, so the key gates it.
        if (RETRYABLE_STATUS.has(response.status) &&
            !options.noRetry &&
            (method === "GET" || idempotencyKey)) {
            await new Promise((resolve) => setTimeout(resolve, 400));
            try {
                response = await fetch(`${this.baseUrl}${path}`, init);
            }
            catch {
                throw new OfflineError();
            }
        }
        if (response.status === 204)
            return undefined;
        const text = await response.text();
        const payload = text ? safeJson(text) : null;
        if (!response.ok) {
            const envelope = payload?.error;
            throw new ApiError(response.status, envelope ?? {
                code: String(response.status),
                message: humanStatus(response.status),
            });
        }
        return payload;
    }
    // -- projects -----------------------------------------------------------
    listProjects(limit = 50) {
        return this.request(`/v1/projects?limit=${limit}`);
    }
    createProject(body) {
        return this.request("/v1/projects", { method: "POST", body });
    }
    getProject(projectId) {
        return this.request(`/v1/projects/${projectId}`);
    }
    /** What this project may spend on providers. `null` clears it. */
    setProjectBudget(projectId, budgetUsd) {
        return this.request(`/v1/projects/${projectId}`, {
            method: "PATCH",
            body: { budget_usd: budgetUsd },
        });
    }
    deleteProject(projectId) {
        return this.request(`/v1/projects/${projectId}`, { method: "DELETE" });
    }
    getStoryboard(projectId) {
        return this.request(`/v1/projects/${projectId}/storyboard`);
    }
    // -- recording and documents -------------------------------------------
    uploadRecording(projectId, audio, extras = {}) {
        const form = new FormData();
        form.append("audio", audio, "recording.webm");
        for (const [key, value] of Object.entries(extras))
            form.append(key, value);
        return this.request(`/v1/projects/${projectId}/recordings`, {
            method: "POST",
            form,
            idempotencyKey: newKey(),
        });
    }
    uploadDocument(projectId, file, extras = {}) {
        const form = new FormData();
        form.append("document", file, file.name);
        for (const [key, value] of Object.entries(extras))
            form.append(key, value);
        return this.request(`/v1/projects/${projectId}/documents`, {
            method: "POST",
            form,
            idempotencyKey: newKey(),
        });
    }
    // -- script -------------------------------------------------------------
    createScript(projectId, text, language) {
        return this.request(`/v1/projects/${projectId}/script`, {
            method: "POST",
            body: language ? { text, language } : { text },
        });
    }
    getScript(projectId) {
        return this.request(`/v1/projects/${projectId}/script`);
    }
    updateBlock(projectId, blockId, text) {
        return this.request(`/v1/projects/${projectId}/script/blocks/${blockId}`, { method: "PATCH", body: { text } });
    }
    proposeRevision(projectId, kind, options = {}) {
        const body = { kind };
        if (options.blockIds?.length)
            body.block_ids = options.blockIds;
        if (options.targetLanguage)
            body.target_language = options.targetLanguage;
        return this.request(`/v1/projects/${projectId}/script/revisions`, {
            method: "POST",
            body,
            idempotencyKey: newKey(),
        });
    }
    getRevision(projectId, revisionId) {
        return this.request(`/v1/projects/${projectId}/script/revisions/${revisionId}`);
    }
    decideRevision(projectId, revisionId, accept) {
        return this.request(`/v1/projects/${projectId}/script/revisions/${revisionId}`, { method: "POST", body: { accept } });
    }
    // -- visual units -------------------------------------------------------
    getUnits(projectId) {
        return this.request(`/v1/projects/${projectId}/visual-units`);
    }
    planUnits(projectId, body = {}) {
        return this.request(`/v1/projects/${projectId}/visual-units`, {
            method: "POST",
            body,
        });
    }
    setUnitState(projectId, unitId, body) {
        return this.request(`/v1/projects/${projectId}/visual-units/${unitId}`, {
            method: "PATCH",
            body,
        });
    }
    regenerate(projectId, unitId, intent) {
        return this.request(`/v1/projects/${projectId}/visual-units/${unitId}/regenerate`, { method: "POST", body: { intent }, idempotencyKey: newKey() });
    }
    // -- timeline -----------------------------------------------------------
    getTimeline(projectId) {
        return this.request(`/v1/projects/${projectId}/timeline`);
    }
    editTimeline(projectId, operations, expectedVersion) {
        const body = { operations };
        if (expectedVersion !== undefined)
            body.expected_version = expectedVersion;
        return this.request(`/v1/projects/${projectId}/timeline`, {
            method: "PATCH",
            body,
        });
    }
    // -- pacing, preview, render -------------------------------------------
    setPacing(projectId, body) {
        return this.request(`/v1/projects/${projectId}/pacing`, {
            method: "POST",
            body,
        });
    }
    preview(projectId, at) {
        return this.request(`/v1/projects/${projectId}/preview?at=${at}`);
    }
    /**
     * Submit a render.
     *
     * `idempotencyKey` is a parameter rather than a fresh random value because a
     * fresh random value defeats the entire mechanism. This minted `newKey()`
     * on every call, so a user double-clicking Render got two genuinely
     * different jobs — the backend's unique index saw two different keys and
     * correctly created two — and the topbar counted up "+1", "+2", "+4". The
     * server was never at fault; the client was asking for a new job each time
     * and then reporting its own surprise.
     *
     * The caller passes a key derived from what actually identifies the work:
     * the scope and the timeline version. Two clicks at the same version are the
     * same request and converge to one job; a click after an edit is a different
     * request and gets its own.
     */
    render(projectId, body, idempotencyKey = newKey()) {
        return this.request(`/v1/projects/${projectId}/render`, {
            method: "POST",
            body,
            idempotencyKey,
        });
    }
    /**
     * Who this key is, and what it may do.
     *
     * The interface uses this to avoid offering actions the caller cannot
     * perform. It is not an authorization check — the server enforces every
     * capability itself and refuses regardless of what the client believes. It is
     * how the interface stops advertising work that will always be refused.
     */
    whoami() {
        return this.request("/v1/me");
    }
    renderHistory(projectId) {
        return this.request(`/v1/projects/${projectId}/renders`);
    }
    videoUrl(projectId) {
        return `${this.baseUrl}/v1/projects/${projectId}/video`;
    }
    captionsUrl(projectId) {
        return `${this.baseUrl}/v1/projects/${projectId}/captions.vtt`;
    }
    /** What this tenant has spent. Read-only; the numbers are the ledger's. */
    usage() {
        return this.request("/v1/usage");
    }
    // -- media --------------------------------------------------------------
    listMedia(projectId) {
        return this.request(`/v1/projects/${projectId}/media`);
    }
    getMedia(projectId, assetId) {
        return this.request(`/v1/projects/${projectId}/media/${assetId}`);
    }
    uploadMedia(projectId, file, kind) {
        const form = new FormData();
        form.append("file", file, file.name);
        if (kind)
            form.append("kind", kind);
        return this.request(`/v1/projects/${projectId}/media`, {
            method: "POST",
            form,
            idempotencyKey: newKey(),
        });
    }
    /**
     * The same upload, reporting bytes as they go.
     *
     * `XMLHttpRequest` rather than `fetch`, and this is the only place in the
     * application that is true. `fetch` has no upload-progress event and will not
     * get one until request streams are universal; a 40 MB video showing a static
     * "Uploading" chip is the difference between "this is working" and "this is
     * broken", so the older API earns its keep here.
     *
     * Everything else — the base URL, the bearer token, the idempotency key, the
     * error envelope — is reproduced deliberately rather than shared, and this
     * comment is the pointer that says so: a change to `request()`'s contract
     * has to be made here too.
     */
    uploadMediaWithProgress(projectId, file, onProgress, kind) {
        const form = new FormData();
        form.append("file", file, file.name);
        if (kind)
            form.append("kind", kind);
        return new Promise((resolve, reject) => {
            const request = new XMLHttpRequest();
            request.open("POST", `${this.baseUrl}/v1/projects/${projectId}/media`);
            if (this.token) {
                request.setRequestHeader("Authorization", `Bearer ${this.token}`);
            }
            request.setRequestHeader("Idempotency-Key", newKey());
            request.upload.addEventListener("progress", (event) => {
                onProgress(event.loaded, event.lengthComputable ? event.total : file.size);
            });
            request.addEventListener("error", () => reject(new OfflineError()));
            request.addEventListener("abort", () => reject(new OfflineError()));
            request.addEventListener("load", () => {
                let parsed = null;
                try {
                    parsed = request.responseText ? JSON.parse(request.responseText) : null;
                }
                catch {
                    parsed = null;
                }
                if (request.status >= 200 && request.status < 300) {
                    resolve(parsed);
                    return;
                }
                const envelope = parsed?.error;
                reject(new ApiError(request.status, envelope ?? {
                    code: "http_error",
                    message: humanStatus(request.status),
                    retryable: request.status >= 500,
                }));
            });
            request.send(form);
        });
    }
    updateMedia(projectId, assetId, body) {
        return this.request(`/v1/projects/${projectId}/media/${assetId}`, {
            method: "PATCH",
            body,
        });
    }
    deleteMedia(projectId, assetId, force = false) {
        return this.request(`/v1/projects/${projectId}/media/${assetId}${force ? "?force=1" : ""}`, { method: "DELETE" });
    }
    useMediaAsVisual(projectId, unitId, assetId, lock = true) {
        return this.request(`/v1/projects/${projectId}/visual-units/${unitId}/media`, { method: "POST", body: { media_asset_id: assetId, lock } });
    }
    // -- jobs ---------------------------------------------------------------
    jobStatus(jobId) {
        return this.request(`/v1/jobs/${jobId}`);
    }
    /**
     * Poll a job until it settles.
     *
     * Reports the queue's own status on every tick — there is no synthetic
     * percentage here, because the queue does not report one and inventing a
     * progress bar that claims 40% is a lie the interface has no way to keep.
     */
    async awaitJob(jobId, options = {}) {
        const interval = options.intervalMs ?? 900;
        const deadline = Date.now() + (options.timeoutMs ?? 15 * 60_000);
        for (;;) {
            if (options.signal?.aborted)
                throw new DOMException("aborted", "AbortError");
            const handle = await this.jobStatus(jobId);
            options.onTick?.(handle);
            if (handle.status === "ready" ||
                handle.status === "failed" ||
                handle.status === "expired" ||
                handle.status === "deleted") {
                return handle;
            }
            if (Date.now() > deadline)
                return handle;
            await new Promise((resolve) => setTimeout(resolve, interval));
        }
    }
    /** The URL of the project event stream. Consumed by `events.ts`. */
    eventsUrl(projectId) {
        return `${this.baseUrl}/v1/projects/${projectId}/events`;
    }
}
function safeJson(text) {
    try {
        return JSON.parse(text);
    }
    catch {
        return null;
    }
}
/**
 * A sentence for the statuses that arrive without an envelope — a proxy 502, a
 * gateway timeout, anything that never reached the application.
 */
function humanStatus(status) {
    if (status === 401)
        return "Your session has expired. Sign in again.";
    if (status === 403)
        return "You do not have access to that.";
    if (status === 404)
        return "We could not find that.";
    if (status === 413)
        return "That file is too large.";
    if (status === 429)
        return "Too many requests just now. Try again in a moment.";
    if (status >= 500)
        return "The server had a problem. Nothing was lost.";
    return "That request could not be completed.";
}
//# sourceMappingURL=client.js.map