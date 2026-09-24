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
export {};
//# sourceMappingURL=types.js.map