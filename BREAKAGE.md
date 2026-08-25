# BREAKAGE.md

Every real bug hit during the build, one line each: what broke, the root cause, the fix.

- 2026-08-25: guard tests exploded with SyntaxError on shop/app.py — a PowerShell `Set-Content -Encoding utf8` edit wrote a UTF-8 BOM that ast.parse rejects; stripped the BOM, guard tests now read files as utf-8-sig, and file edits go through the Write/Edit tools instead of Set-Content (known Round-1 gotcha family).
- 2026-08-25: /order answered malformed JSON bodies with a bare 500 — request.json() was unguarded; all body parsing now goes through a json_body helper returning a typed BAD_REQUEST.
- 2026-08-25: lab scripts emitted SyntaxWarning "\\. is an invalid escape sequence" on Python 3.14 — PowerShell example paths (.\.venv\...) inside ordinary docstrings; fixed by making those docstrings raw strings.
- 2026-08-25: COUPON_INELIGIBLE responses returned `"code": "FRESH150"` instead of the taxonomy code — `VelcrowError.payload()` spread extra fields after the `code` key, so a `code=` kwarg shadowed the taxonomy; fixed by spreading fields first (taxonomy keys win) and renaming the field to `coupon_code`.

