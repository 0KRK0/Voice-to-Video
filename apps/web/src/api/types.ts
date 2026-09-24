/**
 * The wire types, transcribed from the backend's own view functions.
 *
 * Hand-written rather than generated, deliberately. The backend's views are
 * curated — `_unit_view`, `_asset_view` and the rest decide what leaves the
 * building — and a generator pointed at the pydantic models would produce the
 * *internal* shapes, including fields the API is careful not to send. These
 * types describe the responses that actually exist.
 *
 * Every name here corresponds to a `_*_view` in `src/vtv/api/product.py`,
 * `src/vtv/api/media.py` or a handler in `src/vtv/api/app.py`. When one of
 * those changes, `apps/web/tests/contract.test.mjs` fails, which is the point.
 */

// -- Shared -----------------------------------------------------------------

/** The one error envelope every endpoint uses. */
export interface ApiErrorBody {
  code: string;
  message: string;
  retryable?: boolean;
  /**
   * The engineer-facing message, present only when the server is not in
   * production. `message` is written for the person who spoke into the
   * microphone and is incapable of naming a provider, a prompt or a stack
   * frame — which is right, and which left a developer running this locally
   * with "Something went wrong on our side." and nowhere to go while the real
   * exception sat unread in the queue database.
   */
  detail?: string;
}

export type Seconds = number;

/** The answer from `GET /v1/me`. */
export interface Principal {
  subject: string;
  kind: string;
  organisation_id: string | null;
  role: string | null;
  /** Capability strings, e.g. `"project:delete"`. */
  capabilities: string[];
}

// -- Script -----------------------------------------------------------------

export type ScriptOrigin = "spoken" | "authored" | "derived" | "generated";
export type BlockStatus = "draft" | "approved" | "muted";

export interface ScriptBlock {
  block_id: string;
  order: number;
  text: string;
  status: BlockStatus;
  estimated_seconds: Seconds;
  start: Seconds | null;
  end: Seconds | null;
  visual_unit_id: string | null;
  timing_invalidated: boolean;
}

export interface Script {
  script_id: string;
  version: number;
  origin: ScriptOrigin;
  language: string;
  estimated_seconds: Seconds;
  measured_seconds: Seconds | null;
  word_count: number;
  has_stale_timing: boolean;
  /** The audio says something the captions will not. Never hidden. */
  diverged_from_recording: boolean;
  blocks: ScriptBlock[];
}

export interface ScriptEditResult extends Script {
  timing_invalidated_units: string[];
}

export type RevisionKind =
  | "fix_grammar"
  | "improve_clarity"
  | "enhance"
  | "shorten"
  | "expand"
  | "make_formal"
  | "make_cinematic"
  | "make_educational"
  | "make_concise"
  | "translate";

export type RevisionStatus = "proposed" | "accepted" | "rejected" | "superseded";

export interface TextChange {
  block_id: string;
  original: string;
  proposed: string;
  reason: string;
  is_change: boolean;
}

export interface RevisionProposal {
  revision_id: string;
  kind: RevisionKind;
  target_language: string | null;
  status: RevisionStatus;
  based_on_version: number;
  script_version: number | null;
  estimated_duration_before: Seconds;
  estimated_duration_after: Seconds;
  duration_delta_seconds: Seconds;
  changes: TextChange[];
}

export interface RevisionDecision {
  revision_id: string;
  status: RevisionStatus;
  script: Script;
}

// -- Visual units -----------------------------------------------------------

export type VisualUnitStatus =
  | "planned"
  | "searching"
  | "generating"
  | "ready"
  | "approved"
  | "locked"
  | "regenerating"
  | "failed"
  | "degraded"
  | "timing_invalidated";

export type RegenerationIntent =
  | "same_idea"
  | "more_cinematic"
  | "more_realistic"
  | "more_educational"
  | "simpler"
  | "use_real_source"
  | "use_animation"
  | "use_generated_image"
  | "use_generated_video"
  | "use_typography";

/**
 * The grounding gate's verdict on one version.
 *
 * These are the backend's own words, from `GroundingStatus` in
 * `contracts/visual_unit.py`. They were once transcribed as
 * `supported`/`unsupported`, which are not values the API can ever send — so
 * every genuinely **grounded** visual fell through to the default branch and
 * the inspector told the user it made no factual claim, about the one verdict
 * the whole grounding gate exists to produce.
 *
 * `apps/web/tests/contract.test.mjs` now asserts this closed set against the
 * live API, the way it already did for `origin`.
 */
export type GroundingStatus =
  | "not_applicable"
  | "pending"
  | "grounded"
  | "refused";

export type ConsistencyStatus =
  | "consistent"
  | "conflicting"
  | "not_applicable";

export interface VisualVersion {
  version_id: string;
  version: number;
  strategy: string;
  intent: RegenerationIntent | null;
  grounding: GroundingStatus;
  consistency: ConsistencyStatus;
  usable: boolean;
  rationale: string;
  /** Customer-facing. Internal margin is never in this payload. */
  cost_usd: number;
  /**
   * Where the picture came from, in the same vocabulary the media library uses.
   *
   * Sent by the server rather than inferred from `strategy`: `existing_asset`
   * covers both a user's upload and a library asset, and those two carry
   * opposite answers to the licence question the badge exists to settle.
   */
  origin: MediaOrigin;
  user_owned: boolean;
  /**
   * The credit line this picture's licence obliges the video to carry, or null.
   *
   * Shown in the inspector so the obligation is visible before publishing
   * rather than after. The renderer draws it over the shot regardless; this is
   * so the person deciding whether to publish knows it is there.
   */
  attribution: string | null;
}

export interface VisualUnit {
  visual_unit_id: string;
  index: number;
  status: VisualUnitStatus;
  locked: boolean;
  detail: string;
  script_block_ids: string[];
  start: Seconds | null;
  end: Seconds | null;
  deliverable: boolean;
  selected_version_id: string | null;
  versions: VisualVersion[];
}

// -- Pacing -----------------------------------------------------------------

export type PacingMode =
  | "natural"
  | "tight"
  | "cinematic"
  | "educational"
  | "fast"
  | "custom";

export type DurationVerdict =
  | "on_target"
  | "filled"
  | "underfilled"
  | "overrun";

export interface FillAllocation {
  strategy: string;
  seconds: Seconds;
  visual_unit_id: string | null;
}

export interface PacingPlan {
  mode: PacingMode;
  verdict: DurationVerdict;
  narration_seconds: Seconds;
  target_seconds: Seconds | null;
  planned_seconds: Seconds;
  shortfall_seconds: Seconds;
  overrun_seconds: Seconds;
  /** True only for `overrun`. The one state that blocks. */
  needs_user_decision: boolean;
  message: string;
  fill: FillAllocation[];
}

// -- Timeline ---------------------------------------------------------------

export type TrackKind =
  | "narration"
  | "visual"
  | "caption"
  | "music"
  | "sfx"
  | "overlay"
  | "broll"
  | "graphics"
  | "secondary_voice"
  | "chapter";

export type ClipSourceKind = "object" | "programmatic" | "empty" | "text";
export type TransitionKind = "cut" | "fade" | "dissolve" | "wipe" | "push";

export interface TimelineClip {
  clip_id: string;
  track_id: string;
  visual_unit_id: string | null;
  start: Seconds;
  end: Seconds;
  duration: Seconds;
  source_kind: ClipSourceKind;
  locked: boolean;
  label: string;
  transition_in: TransitionKind;
  transition_out: TransitionKind;
  /** Linear, 0 to 1. Converted to decibels only for display. */
  gain: number;
}

export interface TimelineTrack {
  track_id: string;
  kind: TrackKind;
  name: string;
  muted: boolean;
  locked: boolean;
  exclusive: boolean;
  /** Generated from the script. Every edit is refused. */
  derived: boolean;
  gaps: { start: Seconds; end: Seconds }[];
  clips: TimelineClip[];
}

/**
 * The join table, materialised server-side.
 *
 * `TIMELINE_UI_SPEC.md` §2 is explicit: use this rather than deriving the
 * relationship from spans. Two implementations of one relationship disagree the
 * first time a clip is split.
 */
export interface TimelineLink {
  visual_unit_id: string | null;
  clip_id: string;
  start: Seconds;
  end: Seconds;
  script_block_ids: string[];
}

export interface EditTimeline {
  edit_timeline_id: string;
  version: number;
  duration: Seconds;
  target_seconds: Seconds | null;
  tracks: TimelineTrack[];
  links: TimelineLink[];
  script_version: number | null;
}

export type OperationKind =
  | "insert"
  | "remove"
  | "move"
  | "trim"
  | "split"
  | "extend"
  | "replace_source"
  | "lock"
  | "unlock"
  | "set_transition"
  | "set_gain"
  | "add_track"
  | "remove_track"
  | "set_track";

export interface TimelineOperation {
  kind: OperationKind;
  clip_id?: string;
  track_id?: string;
  start?: Seconds;
  end?: Seconds;
  at?: Seconds;
  visual_unit_id?: string;
  source_kind?: ClipSourceKind;
  object?: Record<string, unknown>;
  /**
   * A file from this project's library, named by id.
   *
   * The way to put an upload on a lane. The server resolves it to a storage
   * reference, which is why `object` above is never something this client
   * constructs — object keys are a tenant's key space and do not belong in a
   * browser.
   */
  media_asset_id?: string;
  spec?: Record<string, unknown>;
  text?: string;
  transition_in?: TransitionKind;
  transition_out?: TransitionKind;
  transition_seconds?: Seconds;
  gain?: number;
  track_kind?: TrackKind;
  track_name?: string;
  /** SET_TRACK. Omitting one leaves it alone. */
  muted?: boolean;
  track_locked?: boolean;
}

export interface TimelineEditResult {
  version: number;
  changed_clip_ids: string[];
  affected_unit_ids: string[];
  warnings: string[];
  duration: Seconds;
}

// -- Preview and render -----------------------------------------------------

export interface PreviewState {
  at: Seconds;
  duration: Seconds;
  clip: TimelineClip | null;
  visual_unit: VisualUnit | null;
  script_lines: { block_id: string; text: string }[];
}

export type RenderScope = "clip" | "scene" | "range" | "full_project";

export interface RenderRequest {
  scope: RenderScope;
  clip_id?: string;
  visual_unit_id?: string;
  start?: Seconds;
  end?: Seconds;
  /**
   * Where this render should run.
   *
   * `auto` lets the server decide and is the default when the field is absent.
   * `device` is refused when no computer of theirs is available, rather than
   * quietly falling back — somebody who picked their own machine had a reason,
   * and spending their money in the cloud instead is not a smaller failure than
   * saying so.
   */
  execution?: ExecutionTarget;
}

/** What the picker offers. `auto` is a request; it never comes back. */
export type ExecutionTarget = "auto" | "device" | "cloud";

/** One of this account's computers, as the server sees it. */
export interface DeviceSummary {
  device_id: string;
  name: string;
  /** `idle`, `busy`, `offline`, `revoked`. */
  state: string;
  /** Human-readable hardware, e.g. "8 cores, NVIDIA GeForce GTX 1650". */
  summary: string;
  last_seen_at?: string | null;
}

/** What each option in the picker would actually do, asked before the click. */
export interface RenderTarget {
  execution: ExecutionTarget;
  available: boolean;
  /** Where `auto` would land. Null when the option is unavailable. */
  resolves_to: ExecutionTarget | null;
  /** The sentence to show — the reason it would be chosen, or was refused. */
  reason: string;
}

/** A computer waiting for somebody to approve it in a browser. */
export interface PendingPairing {
  user_code: string;
  name: string;
  summary: string;
  expires_at: string;
}

export interface RenderAccepted {
  job_id: string;
  scope: RenderScope;
  start: Seconds;
  end: Seconds | null;
  /** Coverage, not a saving. A scoped render is not cheaper yet. */
  fraction_of_project: number;
  /** What the encode will actually do. Currently always `full_timeline`. */
  encode: string;
  /**
   * Where it is actually running. Never `auto` — that is what was asked for.
   */
  execution?: ExecutionTarget;
  /** The sentence explaining that choice, written for a person to read. */
  execution_reason?: string;
  /**
   * Whether the scope makes this render cheaper. Currently always `false`.
   *
   * Sent explicitly so the interface cannot imply a saving the backend is not
   * making: the region is recorded and the encode is the whole timeline.
   */
  saves_time: boolean;
}

export interface RenderHistoryEntry {
  render_job_id: string;
  status: string;
  progress: number;
  quality: string;
  frame_rate: number;
  duration_seconds: Seconds | null;
  size_bytes: number | null;
  has_output: boolean;
  has_captions: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface RenderHistory {
  renders: RenderHistoryEntry[];
  downloadable_render_job_id: string | null;
}

// -- Jobs -------------------------------------------------------------------

export type JobStatus =
  | "pending"
  | "processing"
  | "ready"
  | "failed"
  | "retrying"
  | "expired"
  | "deleted";

export interface JobHandle {
  job_id: string;
  kind: string;
  status: JobStatus;
  attempt: number;
  project_id: string | null;
  error: ApiErrorBody | null;
}

// -- Projects ---------------------------------------------------------------

export interface ProjectSummary {
  project_id: string;
  title: string | null;
  status: string;
  progress: number;
  outcome: string | null;
  /** The honest one-line state. Computed server-side; never re-derive it. */
  state: string;
  /** The last successful render's length. `null` until something rendered. */
  duration_seconds: Seconds | null;
  current_stage: string | null;
  style: string;
  aspect_ratio: string;
  cost_usd: number;
  created_at: string | null;
  updated_at: string | null;
  expires_at: string | null;
}

export interface ProjectStage {
  stage: string;
  status: string;
  detail: string | null;
}

/** @see VisualFidelity in vtv.contracts.generation */
export type VisualFidelity = "draft" | "standard" | "fine";

export interface ProjectDetail {
  project_id: string;
  title: string | null;
  status: string;
  progress: number;
  current_stage: string | null;
  stages: ProjectStage[];
  cost_usd: number;
  expires_at: string | null;
  /** The project's real style and shape, decided when it was created. */
  style: string;
  aspect_ratio: string;
  /**
   * What this project may spend on providers, or `null` for the deployment's
   * ceiling. Decides how many visuals may be generated and how many are drawn,
   * found in the commons, or set as type.
   */
  budget_usd: number | null;
  /**
   * How good the generated visuals should be. `null` means the deployment's
   * configured tier.
   */
  visual_fidelity: VisualFidelity | null;
  /**
   * What one generated picture costs at each tier, in US dollars, from the
   * provider's own published table.
   *
   * Sent so the studio can answer "what does my budget buy" while the field is
   * still being typed into, without a round trip per keystroke — and so that
   * the number it shows is the same number the render's budget planner uses.
   * A studio that computed this itself would be a second opinion about the
   * user's money.
   */
  price_usd: Record<string, number>;
  /** `status` says a file exists; this says whether it is worth watching. */
  outcome: string | null;
  deliverable: boolean;
  degradations?: string[];
  narration: { has_speech: boolean };
  video_url?: string;
  captions_url?: string;
  storyboard_url?: string;
  duration_seconds?: Seconds;
  /**
   * The timeline version the file on disk encodes.
   *
   * The only honest basis for "timeline changed since this render". Absent on
   * a render made before the field existed, and absence means *cannot tell* —
   * which the editor draws as no warning at all, because a warning that might
   * be wrong teaches people to ignore warnings.
   */
  rendered_timeline_version?: number | null;
  source?: {
    kind: string;
    origin: string;
    parser: string;
    blocks: number;
    extraction_confidence: number;
  };
  error?: { stage: string; message: string };
}

// -- Storyboard (automatic mode) --------------------------------------------

export interface StoryboardScene {
  scene_id: string;
  index: number;
  narration: string;
  start: Seconds;
  end: Seconds;
  strategy?: string;
  status?: string;
  thumbnail_url?: string;
  rationale?: string;
}

export interface Storyboard {
  scenes: StoryboardScene[];
  [key: string]: unknown;
}

// -- Media ------------------------------------------------------------------

export type MediaKind = "image" | "video" | "audio" | "logo" | "vector";

export type MediaOrigin =
  | "user_upload"
  | "programmatic"
  | "licensed_source"
  | "ai_generated";

export type MediaStatus = "uploading" | "ingesting" | "ready" | "refused";

export interface MediaCapabilities {
  visual: boolean;
  overlay: boolean;
  audio_lane: boolean;
  trim: boolean;
  crop: boolean;
  level: boolean;
  loop: boolean;
  project_mark: boolean;
}

export interface MediaTrim {
  in_seconds: Seconds;
  out_seconds: Seconds | null;
  fade_in_seconds: Seconds;
  fade_out_seconds: Seconds;
  gain_db: number;
  duck_db: number;
  loop: boolean;
  use_source_audio: boolean;
}

export interface MediaCrop {
  x: number;
  y: number;
  width: number;
  height: number;
  is_whole_frame: boolean;
}

export interface MediaAsset {
  media_asset_id: string;
  kind: MediaKind;
  origin: MediaOrigin;
  status: MediaStatus;
  filename: string;
  content_type: string;
  size_bytes: number;
  width: number | null;
  height: number | null;
  source_duration_seconds: Seconds | null;
  duration_seconds: Seconds | null;
  usable: boolean;
  user_owned: boolean;
  detail: string;
  used_by_unit_ids: string[];
  capabilities: MediaCapabilities;
  trim: MediaTrim;
  crop: MediaCrop;
  logo: {
    placement: string;
    width_percent: number;
    inset_px: number;
    opacity: number;
  };
  provenance: {
    creator: string;
    licence: string;
    source_name: string;
    source_url: string;
    attribution: string;
  };
  /** Present only on the detail read. Signed and short-lived. */
  url?: string | null;
}

export interface MediaLibraryView {
  assets: MediaAsset[];
  counts: Record<MediaOrigin, number>;
  project_mark_id: string | null;
}

export interface UseMediaResult {
  visual_unit_id: string;
  version_id: string;
  locked: boolean;
  status: VisualUnitStatus;
}

// -- Billing ----------------------------------------------------------------

/**
 * What this tenant has consumed this period.
 *
 * The server strips provider cost and margin before sending — a customer sees
 * what they used and what it costs them, never what it cost the business — so
 * this type is deliberately narrower than the ledger behind it.
 */
export interface UsageReport {
  organisation_id: string;
  period: string;
  plan: string;
  usage: Record<string, { used: number; limit: number | null; remaining: number | null }>;
}

// -- Events -----------------------------------------------------------------

/**
 * One entry from the server-sent event stream.
 *
 * The names are the backend's `EventName` values. The client switches on the
 * ones it acts on and ignores the rest rather than enumerating all of them —
 * a new backend event must not break an older client.
 */
export interface ProjectEvent {
  name: string;
  project_id?: string;
  at?: string;
  cost_usd?: number;
  data?: Record<string, unknown>;
}
