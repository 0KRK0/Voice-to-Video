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

import type {
  ApiErrorBody,
  DeviceSummary,
  EditTimeline,
  JobHandle,
  MediaAsset,
  MediaLibraryView,
  PacingMode,
  PacingPlan,
  PendingPairing,
  PreviewState,
  Principal,
  ProjectDetail,
  ProjectSummary,
  RegenerationIntent,
  RenderAccepted,
  RenderHistory,
  RenderRequest,
  RenderTarget,
  RevisionDecision,
  RevisionKind,
  RevisionProposal,
  Script,
  ScriptEditResult,
  Storyboard,
  TimelineEditResult,
  TimelineOperation,
  UsageReport,
  UseMediaResult,
  VisualFidelity,
  VisualUnit,
} from "./types.js";

/** A failure the server described. Carries the sentence meant for the user. */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryable: boolean;

  constructor(status: number, body: ApiErrorBody) {
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
  get isRefusal(): boolean {
    return this.status === 403;
  }

  /** A stale `expected_version` on a timeline edit. Offer reload, not retry. */
  get isConflict(): boolean {
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

export interface ClientOptions {
  baseUrl?: string;
  token?: string | null;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  form?: FormData;
  idempotencyKey?: string;
  signal?: AbortSignal;
  /** Suppresses the automatic retry. Used by anything that spends money. */
  noRetry?: boolean;
}

/** Codes that are worth trying again without asking the user. */
const RETRYABLE_STATUS = new Set([502, 503, 504]);

function newKey(): string {
  return crypto.randomUUID();
}

export class VtvClient {
  private baseUrl: string;
  private token: string | null;

  constructor(options: ClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "").replace(/\/$/, "");
    this.token = options.token ?? null;
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  hasToken(): boolean {
    return Boolean(this.token);
  }

  // -- transport ----------------------------------------------------------

  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const { method = "GET", body, form, idempotencyKey, signal } = options;

    const headers: Record<string, string> = {};
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    if (body !== undefined) headers["Content-Type"] = "application/json";

    const init: RequestInit = { method, headers };
    if (signal) init.signal = signal;
    if (form) init.body = form;
    else if (body !== undefined) init.body = JSON.stringify(body);

    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, init);
    } catch (cause) {
      if (signal?.aborted) throw cause;
      throw new OfflineError();
    }

    // One retry, only for the three statuses that mean "the server is having a
    // moment", and never for a call that may have already spent money. A retry
    // of a queued job is safe *because* of the idempotency key, but a retry of
    // one without a key is not, so the key gates it.
    if (
      RETRYABLE_STATUS.has(response.status) &&
      !options.noRetry &&
      (method === "GET" || idempotencyKey)
    ) {
      await new Promise((resolve) => setTimeout(resolve, 400));
      try {
        response = await fetch(`${this.baseUrl}${path}`, init);
      } catch {
        throw new OfflineError();
      }
    }

    if (response.status === 204) return undefined as T;

    const text = await response.text();
    const payload = text ? safeJson(text) : null;

    if (!response.ok) {
      const envelope = (payload as { error?: ApiErrorBody } | null)?.error;
      throw new ApiError(
        response.status,
        envelope ?? {
          code: String(response.status),
          message: humanStatus(response.status),
        },
      );
    }
    return payload as T;
  }

  // -- projects -----------------------------------------------------------

  listProjects(limit = 50): Promise<{ projects: ProjectSummary[] }> {
    return this.request(`/v1/projects?limit=${limit}`);
  }

  createProject(body: {
    title?: string;
    style?: string;
    aspect_ratio?: string;
    direction?: string;
    persistence?: string;
  }): Promise<{ project_id: string }> {
    return this.request("/v1/projects", { method: "POST", body });
  }

  getProject(projectId: string): Promise<ProjectDetail> {
    return this.request(`/v1/projects/${projectId}`);
  }

  /** What this project may spend on providers. `null` clears it. */
  /**
   * Change what this project may spend, and what one picture may cost.
   *
   * Both in one call because they are one decision — the budget says how much,
   * the fidelity says what a shot costs, and only together do they say how much
   * of the video is illustrated. Sending them separately would mean a moment
   * where the server holds a combination the user never chose.
   *
   * A field left `undefined` is not sent and not changed; `null` is a real
   * value meaning "use the deployment's default".
   */
  setProjectSpend(
    projectId: string,
    spend: { budget_usd?: number | null; visual_fidelity?: VisualFidelity | null },
  ): Promise<{
    project_id: string;
    budget_usd: number | null;
    visual_fidelity: VisualFidelity | null;
    price_usd: Record<string, number>;
  }> {
    const body: Record<string, unknown> = {};
    if ("budget_usd" in spend) body.budget_usd = spend.budget_usd;
    if ("visual_fidelity" in spend) body.visual_fidelity = spend.visual_fidelity;
    return this.request(`/v1/projects/${projectId}`, { method: "PATCH", body });
  }

  /**
   * Rename a project.
   *
   * A project names itself from the first sentence of its script, so this is
   * how a user disagrees with that guess rather than how a project first gets
   * a name. Sending an empty string clears the name, which lets the next
   * script upload derive a new one — the difference matters, so it is not
   * collapsed into "falsy means leave alone".
   */
  renameProject(projectId: string, title: string): Promise<{ title: string | null }> {
    return this.request(`/v1/projects/${projectId}`, {
      method: "PATCH",
      body: { title },
    });
  }

  deleteProject(projectId: string): Promise<void> {
    return this.request(`/v1/projects/${projectId}`, { method: "DELETE" });
  }

  getStoryboard(projectId: string): Promise<Storyboard> {
    return this.request(`/v1/projects/${projectId}/storyboard`);
  }

  // -- recording and documents -------------------------------------------

  uploadRecording(
    projectId: string,
    audio: Blob,
    extras: Record<string, string> = {},
  ): Promise<{ job_id: string; status: string }> {
    const form = new FormData();
    form.append("audio", audio, "recording.webm");
    for (const [key, value] of Object.entries(extras)) form.append(key, value);
    return this.request(`/v1/projects/${projectId}/recordings`, {
      method: "POST",
      form,
      idempotencyKey: newKey(),
    });
  }

  uploadDocument(
    projectId: string,
    file: File,
    extras: Record<string, string> = {},
  ): Promise<{ job_id: string; status: string }> {
    const form = new FormData();
    form.append("document", file, file.name);
    for (const [key, value] of Object.entries(extras)) form.append(key, value);
    return this.request(`/v1/projects/${projectId}/documents`, {
      method: "POST",
      form,
      idempotencyKey: newKey(),
    });
  }

  // -- script -------------------------------------------------------------

  createScript(
    projectId: string,
    text: string,
    language?: string,
  ): Promise<Script> {
    return this.request(`/v1/projects/${projectId}/script`, {
      method: "POST",
      body: language ? { text, language } : { text },
    });
  }

  getScript(projectId: string): Promise<Script> {
    return this.request(`/v1/projects/${projectId}/script`);
  }

  updateBlock(
    projectId: string,
    blockId: string,
    text: string,
  ): Promise<ScriptEditResult> {
    return this.request(
      `/v1/projects/${projectId}/script/blocks/${blockId}`,
      { method: "PATCH", body: { text } },
    );
  }

  proposeRevision(
    projectId: string,
    kind: RevisionKind,
    options: { blockIds?: string[]; targetLanguage?: string } = {},
  ): Promise<{ job_id: string }> {
    const body: Record<string, unknown> = { kind };
    if (options.blockIds?.length) body.block_ids = options.blockIds;
    if (options.targetLanguage) body.target_language = options.targetLanguage;
    return this.request(`/v1/projects/${projectId}/script/revisions`, {
      method: "POST",
      body,
      idempotencyKey: newKey(),
    });
  }

  getRevision(
    projectId: string,
    revisionId: string,
  ): Promise<RevisionProposal> {
    return this.request(
      `/v1/projects/${projectId}/script/revisions/${revisionId}`,
    );
  }

  decideRevision(
    projectId: string,
    revisionId: string,
    accept: boolean,
  ): Promise<RevisionDecision> {
    return this.request(
      `/v1/projects/${projectId}/script/revisions/${revisionId}`,
      { method: "POST", body: { accept } },
    );
  }

  // -- visual units -------------------------------------------------------

  getUnits(projectId: string): Promise<{ units: VisualUnit[] }> {
    return this.request(`/v1/projects/${projectId}/visual-units`);
  }

  planUnits(
    projectId: string,
    body: { pacing?: PacingMode; target_seconds?: number } = {},
  ): Promise<{
    units: VisualUnit[];
    pacing: PacingPlan;
    timeline: { version: number; duration: number; clips: number };
  }> {
    return this.request(`/v1/projects/${projectId}/visual-units`, {
      method: "POST",
      body,
    });
  }

  setUnitState(
    projectId: string,
    unitId: string,
    body: { locked?: boolean; approved?: boolean; version_id?: string },
  ): Promise<VisualUnit> {
    return this.request(`/v1/projects/${projectId}/visual-units/${unitId}`, {
      method: "PATCH",
      body,
    });
  }

  regenerate(
    projectId: string,
    unitId: string,
    intent: RegenerationIntent,
  ): Promise<{ job_id: string; visual_unit_id: string }> {
    return this.request(
      `/v1/projects/${projectId}/visual-units/${unitId}/regenerate`,
      { method: "POST", body: { intent }, idempotencyKey: newKey() },
    );
  }

  // -- timeline -----------------------------------------------------------

  getTimeline(projectId: string): Promise<EditTimeline> {
    return this.request(`/v1/projects/${projectId}/timeline`);
  }

  editTimeline(
    projectId: string,
    operations: TimelineOperation[],
    expectedVersion?: number,
  ): Promise<TimelineEditResult> {
    const body: Record<string, unknown> = { operations };
    if (expectedVersion !== undefined) body.expected_version = expectedVersion;
    return this.request(`/v1/projects/${projectId}/timeline`, {
      method: "PATCH",
      body,
    });
  }

  // -- pacing, preview, render -------------------------------------------

  setPacing(
    projectId: string,
    body: { pacing?: PacingMode; target_seconds?: number | null },
  ): Promise<{ pacing: PacingPlan; timeline: { version: number } }> {
    return this.request(`/v1/projects/${projectId}/pacing`, {
      method: "POST",
      body,
    });
  }

  preview(projectId: string, at: number): Promise<PreviewState> {
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
  render(
    projectId: string,
    body: RenderRequest,
    idempotencyKey: string = newKey(),
  ): Promise<RenderAccepted> {
    return this.request(`/v1/projects/${projectId}/render`, {
      method: "POST",
      body,
      idempotencyKey,
    });
  }

  // -- this account's own computers --------------------------------------

  /**
   * The computers paired to this account.
   *
   * Needs `DEVICE_MANAGE`, which is deliberately narrower than the capability
   * for *choosing* where a render runs: deciding which machines an
   * organisation's material may be sent to is administrative, and pressing
   * Render is not.
   */
  devices(): Promise<{ devices: DeviceSummary[] }> {
    return this.request(`/v1/devices`);
  }

  /** Stop a computer rendering for this account. Immediate and one-way. */
  revokeDevice(deviceId: string): Promise<DeviceSummary> {
    return this.request(`/v1/devices/${deviceId}`, { method: "DELETE" });
  }

  /**
   * What each execution option would do right now, without queueing anything.
   *
   * So the picker can show the consequence before the click — "Auto → Rajesh's
   * PC" rather than three radio buttons and a shrug — and can grey out an
   * option *with the sentence saying why*, which is the part that stops the
   * disabled control being a mystery.
   */
  renderTargets(projectId: string): Promise<{ options: RenderTarget[] }> {
    return this.request(`/v1/projects/${projectId}/render/where`);
  }

  /**
   * What a computer is asking to be approved for.
   *
   * The approval screen shows this so the confirmation carries something a
   * person can check — the machine's name and its hardware — rather than a bare
   * Yes, which everybody clicks.
   */
  pendingPairing(userCode: string): Promise<PendingPairing> {
    return this.request(
      `/v1/devices/pair/requests/${encodeURIComponent(userCode)}`,
    );
  }

  /**
   * Say yes, and thereby say which account the computer joins.
   *
   * This is the whole of the authorisation for a browser sign-in: the request
   * arrived belonging to nobody, and the signed-in person approving it is what
   * decides whose it is.
   */
  approvePairing(userCode: string): Promise<{ approved: boolean }> {
    return this.request(`/v1/devices/pair/approve`, {
      method: "POST",
      body: { code: userCode },
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
  whoami(): Promise<Principal> {
    return this.request("/v1/me");
  }

  renderHistory(projectId: string): Promise<RenderHistory> {
    return this.request(`/v1/projects/${projectId}/renders`);
  }

  videoUrl(projectId: string): string {
    return `${this.baseUrl}/v1/projects/${projectId}/video`;
  }

  captionsUrl(projectId: string): string {
    return `${this.baseUrl}/v1/projects/${projectId}/captions.vtt`;
  }

  /** What this tenant has spent. Read-only; the numbers are the ledger's. */
  usage(): Promise<UsageReport> {
    return this.request("/v1/usage");
  }

  // -- media --------------------------------------------------------------

  listMedia(projectId: string): Promise<MediaLibraryView> {
    return this.request(`/v1/projects/${projectId}/media`);
  }

  getMedia(projectId: string, assetId: string): Promise<MediaAsset> {
    return this.request(`/v1/projects/${projectId}/media/${assetId}`);
  }

  uploadMedia(
    projectId: string,
    file: File,
    kind?: "logo",
  ): Promise<MediaAsset> {
    const form = new FormData();
    form.append("file", file, file.name);
    if (kind) form.append("kind", kind);
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
  uploadMediaWithProgress(
    projectId: string,
    file: File,
    onProgress: (sent: number, total: number) => void,
    kind?: "logo",
  ): Promise<MediaAsset> {
    const form = new FormData();
    form.append("file", file, file.name);
    if (kind) form.append("kind", kind);

    return new Promise<MediaAsset>((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("POST", `${this.baseUrl}/v1/projects/${projectId}/media`);
      if (this.token) {
        request.setRequestHeader("Authorization", `Bearer ${this.token}`);
      }
      request.setRequestHeader("Idempotency-Key", newKey());

      request.upload.addEventListener("progress", (event) => {
        onProgress(event.loaded, event.lengthComputable ? event.total : file.size);
      });
      request.addEventListener("error", () =>
        reject(new OfflineError()),
      );
      request.addEventListener("abort", () =>
        reject(new OfflineError()),
      );
      request.addEventListener("load", () => {
        let parsed: unknown = null;
        try {
          parsed = request.responseText ? JSON.parse(request.responseText) : null;
        } catch {
          parsed = null;
        }
        if (request.status >= 200 && request.status < 300) {
          resolve(parsed as MediaAsset);
          return;
        }
        const envelope = (parsed as { error?: ApiErrorBody } | null)?.error;
        reject(
          new ApiError(
            request.status,
            envelope ?? {
              code: "http_error",
              message: humanStatus(request.status),
              retryable: request.status >= 500,
            },
          ),
        );
      });
      request.send(form);
    });
  }

  updateMedia(
    projectId: string,
    assetId: string,
    body: Record<string, unknown>,
  ): Promise<MediaAsset> {
    return this.request(`/v1/projects/${projectId}/media/${assetId}`, {
      method: "PATCH",
      body,
    });
  }

  deleteMedia(
    projectId: string,
    assetId: string,
    force = false,
  ): Promise<{ deleted: string }> {
    return this.request(
      `/v1/projects/${projectId}/media/${assetId}${force ? "?force=1" : ""}`,
      { method: "DELETE" },
    );
  }

  useMediaAsVisual(
    projectId: string,
    unitId: string,
    assetId: string,
    lock = true,
  ): Promise<UseMediaResult> {
    return this.request(
      `/v1/projects/${projectId}/visual-units/${unitId}/media`,
      { method: "POST", body: { media_asset_id: assetId, lock } },
    );
  }

  // -- jobs ---------------------------------------------------------------

  jobStatus(jobId: string): Promise<JobHandle> {
    return this.request(`/v1/jobs/${jobId}`);
  }

  /**
   * Poll a job until it settles.
   *
   * Reports the queue's own status on every tick — there is no synthetic
   * percentage here, because the queue does not report one and inventing a
   * progress bar that claims 40% is a lie the interface has no way to keep.
   */
  async awaitJob(
    jobId: string,
    options: {
      onTick?: (handle: JobHandle) => void;
      signal?: AbortSignal;
      intervalMs?: number;
      timeoutMs?: number;
    } = {},
  ): Promise<JobHandle> {
    const interval = options.intervalMs ?? 900;
    const deadline = Date.now() + (options.timeoutMs ?? 15 * 60_000);

    for (;;) {
      if (options.signal?.aborted) throw new DOMException("aborted", "AbortError");
      const handle = await this.jobStatus(jobId);
      options.onTick?.(handle);
      if (
        handle.status === "ready" ||
        handle.status === "failed" ||
        handle.status === "expired" ||
        handle.status === "deleted"
      ) {
        return handle;
      }
      if (Date.now() > deadline) return handle;
      await new Promise((resolve) => setTimeout(resolve, interval));
    }
  }

  /** The URL of the project event stream. Consumed by `events.ts`. */
  eventsUrl(projectId: string): string {
    return `${this.baseUrl}/v1/projects/${projectId}/events`;
  }
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/**
 * A sentence for the statuses that arrive without an envelope — a proxy 502, a
 * gateway timeout, anything that never reached the application.
 */
function humanStatus(status: number): string {
  if (status === 401) return "Your session has expired. Sign in again.";
  if (status === 403) return "You do not have access to that.";
  if (status === 404) return "We could not find that.";
  if (status === 413) return "That file is too large.";
  if (status === 429) return "Too many requests just now. Try again in a moment.";
  if (status >= 500) return "The server had a problem. Nothing was lost.";
  return "That request could not be completed.";
}
