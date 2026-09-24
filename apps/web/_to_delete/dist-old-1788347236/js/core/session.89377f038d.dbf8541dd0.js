/**
 * Who the user is, and where the API lives.
 *
 * The backend authenticates with a bearer API key. There is no cookie session
 * and no refresh flow, so this module's whole job is to hold one credential,
 * make it available to the client, and be honest about the fact that the
 * frontend is not the thing enforcing anything.
 *
 * ## The frontend is not a security boundary
 *
 * Nothing here decides what a user may do. Tenant isolation, authorisation,
 * project ownership, locks and quotas are all enforced server-side, and every
 * one of them answers with a refusal this client displays. The token is held
 * only so that requests can carry it; hiding a button is a courtesy, never a
 * control.
 */
import { signal } from "/js/core/store.898a47de9e.898a47de9e.js";
const TOKEN_KEY = "vtv.token";
const BASE_KEY = "vtv.baseUrl";
/**
 * Where the API is.
 *
 * Same origin by default — the intended deployment serves this app from the
 * application itself, which removes CORS from the picture entirely. An explicit
 * base URL exists for the case where the two are split across hosts, and for
 * the browser tests, which run the app from a file server against a real API on
 * another port.
 */
function initialBaseUrl() {
    const fromQuery = new URLSearchParams(window.location.search).get("api");
    if (fromQuery) {
        window.localStorage.setItem(BASE_KEY, fromQuery);
        return fromQuery.replace(/\/$/, "");
    }
    const stored = window.localStorage.getItem(BASE_KEY);
    if (stored)
        return stored.replace(/\/$/, "");
    const meta = document.querySelector('meta[name="vtv-api"]');
    if (meta?.content)
        return meta.content.replace(/\/$/, "");
    return "";
}
function initialToken() {
    // A token supplied in the query is consumed and removed from the address bar
    // immediately: a credential in a URL ends up in history, in referrer headers
    // and in screen shares.
    const params = new URLSearchParams(window.location.search);
    const fromQuery = params.get("token");
    if (fromQuery) {
        window.localStorage.setItem(TOKEN_KEY, fromQuery);
        params.delete("token");
        const rest = params.toString();
        window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : "") + window.location.hash);
        return fromQuery;
    }
    return window.localStorage.getItem(TOKEN_KEY);
}
export const session = signal({
    token: initialToken(),
    baseUrl: initialBaseUrl(),
});
export function signIn(token) {
    const clean = token.trim();
    window.localStorage.setItem(TOKEN_KEY, clean);
    session.update((state) => ({ ...state, token: clean }));
}
export function signOut() {
    window.localStorage.removeItem(TOKEN_KEY);
    session.update((state) => ({ ...state, token: null }));
}
export function setBaseUrl(url) {
    const clean = url.trim().replace(/\/$/, "");
    window.localStorage.setItem(BASE_KEY, clean);
    session.update((state) => ({ ...state, baseUrl: clean }));
}
//# sourceMappingURL=session.js.map