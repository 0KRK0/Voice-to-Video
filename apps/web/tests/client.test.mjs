/**
 * The transport.
 *
 * `VtvClient.request()` is the only place this application talks to a server,
 * which is what makes authentication, the idempotency header, the error
 * envelope and the retry rule *guarantees* rather than conventions. Each of
 * those four is tested here against a stubbed `fetch`, because each of them is
 * something a future caller could quietly opt out of by writing its own call.
 *
 * The retry rule is the one worth being careful about: a retry of a request
 * that may already have spent money is a double charge, and the thing that
 * makes a retry safe is the idempotency key — so the key gates the retry.
 */

import { strict as assert } from "node:assert";
import { test } from "node:test";

const { ApiError, OfflineError, VtvClient } = await import(
  "../dist-test/js/api/client.js"
);

/** Replace global fetch for one test, restoring it afterwards. */
function withFetch(impl, run) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init: init ?? {} });
    return impl(calls.length, { url: String(url), init: init ?? {} });
  };
  return Promise.resolve(run(calls)).finally(() => {
    globalThis.fetch = original;
  });
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("the bearer token goes on every request", async () => {
  const client = new VtvClient({ baseUrl: "https://api.test", token: "vtv_abc" });
  await withFetch(
    () => json({ projects: [] }),
    async (calls) => {
      await client.listProjects();
      assert.equal(calls[0].init.headers.Authorization, "Bearer vtv_abc");
    },
  );
});

test("no token means no Authorization header, not an empty one", async () => {
  // `Bearer ` with nothing after it is a malformed credential, and the server
  // would answer 401 rather than the 403-with-a-sentence an anonymous read
  // should get.
  const client = new VtvClient({ baseUrl: "https://api.test" });
  await withFetch(
    () => json({ status: "ok" }),
    async (calls) => {
      await client.request("/health");
      assert.equal(calls[0].init.headers.Authorization, undefined);
    },
  );
});

test("setToken changes what the next request sends", async () => {
  const client = new VtvClient({ baseUrl: "https://api.test", token: "old" });
  client.setToken("new");
  await withFetch(
    () => json({ projects: [] }),
    async (calls) => {
      await client.listProjects();
      assert.equal(calls[0].init.headers.Authorization, "Bearer new");
    },
  );
});

test("anything that spends money carries an idempotency key", async () => {
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  await withFetch(
    () => json({ job_id: "prj_x", visual_unit_id: "vun_y" }, 202),
    async (calls) => {
      await client.regenerate("prj_1", "vun_1", "same_idea");
      const key = calls[0].init.headers["Idempotency-Key"];
      assert.ok(key, "a regeneration must be idempotent — it is charged for");
      assert.match(key, /^[0-9a-f-]{36}$/);
    },
  );
});

test("a refusal is surfaced verbatim, never paraphrased", async () => {
  // Every refusal in this system comes with a sentence naming its remedy, and
  // rewording it client-side throws away the most useful part.
  const sentence =
    "That would overlap another clip on the visual track. Move or trim clip 05 first.";
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });

  await withFetch(
    () => json({ error: { code: "schema_invalid", message: sentence, retryable: false } }, 403),
    async () => {
      await assert.rejects(
        () => client.getTimeline("prj_1"),
        (error) => {
          assert.ok(error instanceof ApiError);
          assert.equal(error.message, sentence);
          assert.equal(error.code, "schema_invalid");
          assert.equal(error.status, 403);
          assert.equal(error.retryable, false);
          return true;
        },
      );
    },
  );
});

test("a 403 is a refusal; a 400 with the same code is a malformed request", async () => {
  // Both arrive as `schema_invalid` and only the status separates them. The
  // editor needs the distinction: one means "not allowed, here is why", the
  // other means "the client sent nonsense".
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });

  await withFetch(
    () => json({ error: { code: "schema_invalid", message: "no" } }, 403),
    async () => {
      const error = await client.getTimeline("p").catch((e) => e);
      assert.equal(error.isRefusal, true);
    },
  );
  await withFetch(
    () => json({ error: { code: "schema_invalid", message: "no" } }, 400),
    async () => {
      const error = await client.getTimeline("p").catch((e) => e);
      assert.equal(error.isRefusal, false);
    },
  );
});

test("a stale-version refusal is recognised as a conflict, so reload is offered", async () => {
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  await withFetch(
    () =>
      json(
        {
          error: {
            code: "permission_denied",
            message: "Someone else changed this timeline. Reload and try again.",
          },
        },
        403,
      ),
    async () => {
      const error = await client.getTimeline("p").catch((e) => e);
      assert.equal(error.isConflict, true, "a conflict must offer reload, not retry");
    },
  );
});

test("a body with no error envelope still produces a readable message", async () => {
  // A proxy returning a bare 502 does not know about this application's
  // envelope. The user must not be shown "undefined".
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  await withFetch(
    () => new Response("<html>gateway</html>", { status: 500 }),
    async () => {
      const error = await client.getTimeline("p").catch((e) => e);
      assert.ok(error instanceof ApiError);
      assert.ok(error.message.length > 0);
      assert.ok(!/undefined/.test(error.message));
    },
  );
});

test("a network failure is an OfflineError, distinct from a refusal", async () => {
  // The editor shows these differently: a refusal is the user's to act on, a
  // network failure is the network's, and conflating them produces "your edit
  // was not allowed" when the wifi dropped.
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  await withFetch(
    () => {
      throw new TypeError("Failed to fetch");
    },
    async () => {
      const error = await client.getTimeline("p").catch((e) => e);
      assert.ok(error instanceof OfflineError);
      assert.ok(!(error instanceof ApiError));
    },
  );
});

test("a 503 on a GET is retried once and then succeeds", async () => {
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  await withFetch(
    (attempt) =>
      attempt === 1
        ? json({ error: { code: "unavailable", message: "busy" } }, 503)
        : json({ projects: [] }),
    async (calls) => {
      const result = await client.listProjects();
      assert.deepEqual(result, { projects: [] });
      assert.equal(calls.length, 2, "one retry, not a loop");
    },
  );
});

test("a 503 on a POST without an idempotency key is NOT retried", async () => {
  // This is the rule that stops a double charge. A retry is safe *because* of
  // the key; without one the request may already have had an effect.
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  await withFetch(
    () => json({ error: { code: "unavailable", message: "busy" } }, 503),
    async (calls) => {
      await client
        .editTimeline("p", [{ kind: "lock", clip_id: "c" }], 1)
        .catch(() => undefined);
      assert.equal(calls.length, 1, "an un-keyed write must not be replayed");
    },
  );
});

test("a 500 is never retried, keyed or not", async () => {
  // 500 means the server broke on this input. Sending it again breaks it again.
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  await withFetch(
    () => json({ error: { code: "internal_error", message: "boom" } }, 500),
    async (calls) => {
      await client.listProjects().catch(() => undefined);
      assert.equal(calls.length, 1);
    },
  );
});

test("a retried keyed request reuses the same key", async () => {
  // A retry with a *new* key is exactly the double charge the key exists to
  // prevent — it looks like a second, different request to the queue.
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  await withFetch(
    (attempt) =>
      attempt === 1
        ? json({ error: { code: "unavailable", message: "busy" } }, 503)
        : json({ job_id: "prj_x" }, 202),
    async (calls) => {
      await client.regenerate("p", "u", "same_idea");
      assert.equal(calls.length, 2);
      assert.equal(
        calls[0].init.headers["Idempotency-Key"],
        calls[1].init.headers["Idempotency-Key"],
      );
    },
  );
});

test("a 204 resolves rather than failing to parse an empty body", async () => {
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  await withFetch(
    () => new Response(null, { status: 204 }),
    async () => {
      assert.equal(await client.deleteProject("p"), undefined);
    },
  );
});

test("the base URL is normalised, so a trailing slash cannot double up", async () => {
  const client = new VtvClient({ baseUrl: "https://api.test/", token: "k" });
  await withFetch(
    () => json({ projects: [] }),
    async (calls) => {
      await client.listProjects();
      assert.equal(calls[0].url, "https://api.test/v1/projects?limit=50");
    },
  );
});

test("awaitJob stops on a terminal state and returns it", async () => {
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  await withFetch(
    (attempt) =>
      json({
        job_id: "prj_x",
        kind: "render_scope",
        status: attempt < 3 ? "processing" : "ready",
        attempt: 1,
      }),
    async (calls) => {
      const handle = await client.awaitJob("prj_x", { intervalMs: 1 });
      assert.equal(handle.status, "ready");
      assert.equal(calls.length, 3, "polling stops the moment it is terminal");
    },
  );
});

test("awaitJob returns a failure rather than throwing it", async () => {
  // The caller needs the error envelope to show the user *why* a render
  // failed. Throwing loses it into the generic handler.
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  await withFetch(
    () =>
      json({
        job_id: "prj_x",
        kind: "render_scope",
        status: "failed",
        attempt: 3,
        error: { code: "render_failed", message: "ffmpeg gave up", retryable: false },
      }),
    async () => {
      const handle = await client.awaitJob("prj_x", { intervalMs: 1 });
      assert.equal(handle.status, "failed");
      assert.equal(handle.error.message, "ffmpeg gave up");
    },
  );
});

test("an aborted awaitJob throws AbortError rather than resolving", async () => {
  const client = new VtvClient({ baseUrl: "https://api.test", token: "k" });
  const controller = new AbortController();
  controller.abort();
  await withFetch(
    () => json({ job_id: "x", status: "processing" }),
    async () => {
      await assert.rejects(
        () => client.awaitJob("x", { signal: controller.signal, intervalMs: 1 }),
        (error) => error.name === "AbortError",
      );
    },
  );
});
