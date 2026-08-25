# BREAKAGE.md

Every real bug hit during the build, one line each: what broke, the root cause, the fix.

- 2026-08-25: guard tests exploded with SyntaxError on shop/app.py — a PowerShell `Set-Content -Encoding utf8` edit wrote a UTF-8 BOM that ast.parse rejects; stripped the BOM, guard tests now read files as utf-8-sig, and file edits go through the Write/Edit tools instead of Set-Content (known Round-1 gotcha family).
- 2026-08-25: /order answered malformed JSON bodies with a bare 500 — request.json() was unguarded; all body parsing now goes through a json_body helper returning a typed BAD_REQUEST.
- 2026-08-25: lab scripts emitted SyntaxWarning "\\. is an invalid escape sequence" on Python 3.14 — PowerShell example paths (.\.venv\...) inside ordinary docstrings; fixed by making those docstrings raw strings.
- 2026-08-25: COUPON_INELIGIBLE responses returned `"code": "FRESH150"` instead of the taxonomy code — `VelcrowError.payload()` spread extra fields after the `code` key, so a `code=` kwarg shadowed the taxonomy; fixed by spreading fields first (taxonomy keys win) and renaming the field to `coupon_code`.

- 2026-08-25: writing Checkout.jsx through a quoted bash heredoc died with "unexpected EOF while looking for matching `''" — this harness mangles apostrophes inside heredocs (`human's`, `shop's`), a known Round-1 gotcha; wrote that file with the Write tool instead.
- 2026-08-26: the storefront showed its "could not reach the shop API" error state in Chrome while curl succeeded — uvicorn binds IPv4 127.0.0.1 only and Chrome resolves `localhost` to IPv6 ::1 first, so every browser fetch failed; pinned the frontend API bases to 127.0.0.1 and added the 127.0.0.1 storefront origins to both CORS allowlists.
