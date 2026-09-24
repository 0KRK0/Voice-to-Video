/**
 * Turning a server refusal into something on screen.
 *
 * One place, because the mapping in `EDITOR_INTERACTION_SPEC.md` §14 is a
 * contract and forty call sites each deciding whether to show a retry button
 * would honour it for a while and then stop.
 *
 * The single most important rule here: **the server's `message` is shown
 * verbatim.** Every refusal in this system is written for a person and names
 * the remedy — "That clip is locked. Unlock it to change it." Paraphrasing
 * throws away the useful half.
 */
import { ApiError, OfflineError } from "/js/api/client.04d34272e9.04d34272e9.js";
import { toast } from "/js/widgets/dialog.e684c71873.cd02d66442.js";
/**
 * Classify a thrown value.
 *
 * Never invents a message. When the server did not supply one — a proxy 502, a
 * dropped connection — the sentence comes from a short table rather than from
 * the exception's text, because an exception's text is written for a log.
 */
export function present(error) {
    if (error instanceof OfflineError) {
        return {
            message: "We could not reach the server. Your work is saved locally until it comes back.",
            retryable: true,
            remedy: "none",
            tone: "warn",
        };
    }
    if (error instanceof ApiError) {
        if (error.isConflict) {
            return {
                message: error.message,
                retryable: false,
                remedy: "reload",
                tone: "warn",
            };
        }
        if (error.status === 401 || error.code === "not_authenticated") {
            return {
                message: error.message,
                retryable: false,
                remedy: "sign-in",
                tone: "error",
            };
        }
        if (error.code === "permission_denied" && /lock/i.test(error.message)) {
            return {
                message: error.message,
                retryable: false,
                remedy: "unlock",
                tone: "warn",
            };
        }
        if (error.code === "quota_exceeded" || error.code === "budget_exceeded") {
            return {
                message: error.message,
                retryable: false,
                remedy: "upgrade",
                tone: "warn",
            };
        }
        return {
            message: error.message,
            // `retryable: false` means the retry button must not be shown. A retry on
            // a non-retryable refusal invites the user to click something that will
            // fail identically.
            retryable: error.retryable,
            remedy: "none",
            tone: error.status >= 500 ? "error" : "warn",
        };
    }
    if (error instanceof DOMException && error.name === "AbortError") {
        return { message: "", retryable: false, remedy: "none", tone: "warn" };
    }
    return {
        message: "Something went wrong. Nothing was lost.",
        retryable: true,
        remedy: "none",
        tone: "error",
    };
}
/** Show a failure that has no better home than the toast rail. */
export function reportError(error, options = {}) {
    const presented = present(error);
    if (!presented.message)
        return presented;
    const action = presented.remedy === "reload" && options.onReload
        ? { label: "Reload", onClick: options.onReload }
        : presented.retryable && options.retry
            ? { label: "Try again", onClick: options.retry }
            : undefined;
    toast(presented.message, {
        tone: presented.tone,
        ...(action ? { action } : {}),
    });
    return presented;
}
/**
 * Run something, reporting any failure and returning `undefined` instead of
 * throwing.
 *
 * The shape of nearly every handler in the app: the failure is the user's to
 * see, not the caller's to unwind.
 */
export async function attempt(run, options = {}) {
    try {
        return await run();
    }
    catch (error) {
        if (options.onError) {
            const presented = present(error);
            if (presented.message)
                options.onError(presented.message);
            else
                reportError(error, options);
        }
        else {
            reportError(error, options);
        }
        return undefined;
    }
}
//# sourceMappingURL=errors.js.map