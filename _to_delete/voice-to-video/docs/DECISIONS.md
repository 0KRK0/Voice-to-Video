# Decisions

Choices made during Stage 0 that a reader might otherwise assume were accidents,
each with the reasoning and the alternative that was rejected.

---

## D1 — The strategy vocabulary is split in two

**Decision.** `VisualStrategy` answers *how do we obtain this visual*
(existing / programmatic / licensed / generated image / generated video /
composite). `VisualPrimitive` answers *what do we draw* (typography, chart,
timeline, network, comparison, map).

**Why.** The brief lists eleven flat strategies, six of which — diagram, chart,
timeline, map, typography, animation — are things our own engine draws, not ways
of obtaining a visual. Flattening them would force the Director to re-learn the
same "draw it ourselves" decision six times, and would hide the one ratio that
determines our unit economics: drawn versus generated.

**Rejected.** The flat eleven-value enum. The full mapping is preserved in
`docs/VISUAL_DIRECTOR.md`, so nothing from the brief is lost.

---

## D2 — Rights are tri-state and fail closed

**Decision.** `Permission` is `ALLOWED | DENIED | UNKNOWN`, and `UNKNOWN` behaves
as `DENIED` at every commercial decision point.

**Why.** A boolean forces every unparsed licence into either a false permission
or a false prohibition. The tri-state lets us record "we have not established
this" honestly and then behave safely. Commercial use additionally requires
modification rights, because this pipeline modifies everything it touches.

**Rejected.** `commercial_use: bool`.

---

## D3 — `WEB_SCRAPE` exists in the enum so it can be refused by name

**Decision.** The forbidden source is named and then rejected by a validator.

**Why.** Naming it makes the prohibition testable. Omitting it would mean scraped
material eventually arrives mislabelled as something permitted, with nothing to
catch it.

---

## D4 — Animation specs are data; nothing model-authored is executed

**Decision.** A language model selects a primitive and fills in typed fields. It
never emits React, SVG, CSS or any other executable or injectable content.

**Why.** Security — nothing model-authored is run. Quality — every visual the
system can produce is one we designed and tested. Strategy — the vocabulary
becomes a compounding proprietary asset rather than a prompt.

**Cost.** The Director can only express what we have built. That is the point.

---

## D5 — The narration is the only clock

**Decision.** Video duration equals narration duration. Visuals are fitted to the
voice; the voice is never stretched or cut to accommodate a visual.

**Why.** It removes an entire class of desynchronisation bugs. Regenerating a
shot cannot shift a caption, because visuals have no say in timing at all.

**Consequence.** Every clip must declare a `FitPolicy` for what to do when its
media is shorter or longer than its slot. The contract refuses combinations that
cannot fill their slot.

---

## D6 — Programmatic visuals are stored as specifications, not files

**Decision.** `ProgrammaticClipSource` carries the spec; the renderer draws it at
render time.

**Why.** Resolution-independent, instantly re-editable, free to regenerate, and
for a typical explainer it turns roughly two thirds of the project's visual
payload from megabytes of PNG into kilobytes of JSON.

---

## D7 — Keep the reasoning, not the pixels

**Decision.** A saved project stores transcript, understanding, scene graph,
visual plan and timeline. It does not store the finished MP4 by default.

**Why.** Those documents are a few hundred kilobytes and can rebuild the video at
any resolution, in any aspect ratio, with any shot replaced. `Project.is_regenerable`
asserts the property holds.

---

## D8 — Cache keys exclude everything that does not determine the output

**Decision.** `GenerationRequest.cache_key()` hashes kind and params only —
never the request id, timestamps, scene, budget or provider hint.

**Why.** Two scenes needing the same image pay once; re-rendering after an
unrelated edit costs nothing for unchanged shots; evaluation runs are nearly free
after the first pass. At scale this is the difference between a viable margin and
an unviable one.

---

## D9 — Provider data policy is a routing input, not a document

**Decision.** `DataPolicy` defaults to `trains_on_input=True`,
`dpa_in_place=False`, and raw voice may only go to a provider passing
`is_acceptable_for_user_voice`.

**Why.** A privacy commitment enforced by a field comparison at dispatch is a
commitment. One enforced by a paragraph in a policy PDF is an intention.

---

## D10 — One Python package, not a monorepo of empty directories

**Decision.** `src/vtv/{contracts,ports,examples}` today. `services/`,
`packages/`, `apps/`, `infrastructure/` when something goes in them.

**Why.** Empty scaffolding produces a repository that lies about what exists. The
seams that matter are enforced by the import-boundary tests, which work regardless
of directory layout and will keep working when the layout grows.

---

## D11 — Tests are `unittest.TestCase`, run by pytest

**Decision.** Test classes use the standard library's `unittest`; `pytest` is the
configured runner and discovers them natively.

**Why.** The suite runs in any environment, including one where installing pytest
is impossible. That is not hypothetical — Stage 0 was developed in exactly such
an environment — and it cost nothing to guarantee.

---

## D12 — Identifiers are prefixed Crockford base32

**Decision.** `scn_01h9zk3m...`, 24 characters from an alphabet excluding
`i`, `l`, `o` and `u`.

**Why.** The prefix makes logs, traces and database rows self-describing and
makes it impossible to pass a scene id where an asset id belongs. The alphabet
survives being read aloud, retyped from a screenshot, or pasted into a support
ticket. No coordination is required to mint one.

---

## D13 — `extra="forbid"` on every contract

**Decision.** Unknown fields are a validation error everywhere.

**Why.** The single highest-value line of configuration in the contracts. It
turns a hallucinated field from a language model, or a client on a stale schema,
into a loud error at the boundary instead of silently dropped data discovered
missing four stages later.

---

## D14 — Both a `rationale` and an `estimate` on every visual directive

**Decision.** Every decision states why it was made and what it is expected to
cost.

**Why.** The rationale makes decisions reviewable by humans, explainable to users
and gradeable by the evaluation system. The estimate — supplied by the router
from real provider capability data, not guessed by a model — makes the cost
trade-off an actual computation.
