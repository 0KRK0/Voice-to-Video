/**
 * The project event stream.
 *
 * The backend publishes server-sent events at `/v1/projects/{id}/events`, and
 * this is the only consumer. Two things make it more than a thin `EventSource`
 * wrapper:
 *
 * **It carries a credential.** `EventSource` cannot set an `Authorization`
 * header, and this API has no cookie session, so the stream is read with
 * `fetch` and parsed by hand. That is thirty lines of SSE framing in exchange
 * for not inventing a second authentication mechanism for one endpoint.
 *
 * **It reconnects, and says when it cannot.** A stream that silently dies
 * leaves an editor showing "generating" forever. This one retries with a
 * bounded backoff and reports the disconnection so the interface can stop
 * claiming to know what is happening.
 */
/** Names that mean the backend will send nothing further for this project. */
const TERMINAL = new Set([
    "render.completed",
    "render.failed",
    "stage.failed",
]);
export class ProjectEventStream {
    url;
    options;
    controller = null;
    stopped = false;
    attempt = 0;
    constructor(url, options) {
        this.url = url;
        this.options = options;
    }
    start() {
        this.stopped = false;
        void this.run();
    }
    stop() {
        this.stopped = true;
        this.controller?.abort();
        this.controller = null;
    }
    async run() {
        while (!this.stopped) {
            try {
                await this.connect();
                // A clean end of stream means the backend finished. Not an error, and
                // not something to reconnect to.
                if (!this.stopped)
                    this.options.onConnection?.(false);
                return;
            }
            catch (error) {
                if (this.stopped || error?.name === "AbortError")
                    return;
                this.options.onConnection?.(false);
                this.attempt += 1;
                // 1s, 2s, 4s, 8s, then every 15s. Bounded, because a project left open
                // overnight must not hammer the server, and unbounded backoff would
                // mean a stream that never comes back after a laptop wakes up.
                const wait = Math.min(15_000, 1000 * 2 ** Math.min(this.attempt, 3));
                await new Promise((resolve) => window.setTimeout(resolve, wait));
            }
        }
    }
    async connect() {
        this.controller = new AbortController();
        const headers = { Accept: "text/event-stream" };
        if (this.options.token) {
            headers.Authorization = `Bearer ${this.options.token}`;
        }
        const response = await fetch(this.url, {
            headers,
            signal: this.controller.signal,
        });
        if (!response.ok || !response.body) {
            throw new Error(`event stream refused with ${response.status}`);
        }
        this.attempt = 0;
        this.options.onConnection?.(true);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
            const { done, value } = await reader.read();
            if (done)
                return;
            buffer += decoder.decode(value, { stream: true });
            // SSE frames are separated by a blank line. Anything after the last
            // separator is a partial frame and stays in the buffer.
            let separator = buffer.indexOf("\n\n");
            while (separator !== -1) {
                const frame = buffer.slice(0, separator);
                buffer = buffer.slice(separator + 2);
                const event = parseFrame(frame);
                if (event) {
                    this.options.onEvent(event);
                    if (event.name && TERMINAL.has(event.name)) {
                        this.stopped = true;
                        this.controller?.abort();
                        this.options.onConnection?.(false);
                        return;
                    }
                }
                separator = buffer.indexOf("\n\n");
            }
        }
    }
}
function parseFrame(frame) {
    const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
    if (!data)
        return null;
    try {
        const parsed = JSON.parse(data);
        return typeof parsed === "object" && parsed !== null ? parsed : null;
    }
    catch {
        // A malformed frame is the server's problem and not worth tearing the
        // stream down for; the next one will very likely parse.
        return null;
    }
}
//# sourceMappingURL=events.js.map