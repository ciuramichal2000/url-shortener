# shorturl

A small URL shortener service built on the Python standard library: `http.server` for the HTTP layer and SQLite for storage. You POST a long URL (optionally with a custom code), get back a short base62 code, and visiting the code redirects to the target while counting the click. There are no runtime dependencies; `pytest` is only needed to run the tests.

## Interesting engineering

- **Codes come from the row id, not from randomness.** A link's code is `base62(62**3 + row_id)`, so generated codes are unique by construction and there is no generate-and-retry loop. A custom code can equal a code the generator has not issued yet; when that happens the store skips that row id (SQLite `AUTOINCREMENT` never reuses ids), so a custom code can never collide with a generated one.
- **Click counting is a single atomic statement.** `UPDATE ... SET clicks = clicks + 1 ... RETURNING url` finds the target and counts the visit in one step, and it counts nothing for an unknown code. A test sends 20 concurrent visits and expects exactly 20 clicks.
- **Input is checked before it is stored.** Only `http`/`https` URLs are accepted (so `javascript:` and `data:` are refused), URLs are capped at 2048 characters, and whitespace, control characters, backslashes and non-ASCII are refused so the value cannot break out of the `Location` header. Links whose host is the service itself are refused, with every loopback spelling (`localhost`, `127.x.x.x`, `::1`, `0.0.0.0`) treated as the same host.
- **Request size limits answer cleanly.** The body is capped at 4096 bytes and checked from `Content-Length` before it is read. An oversized body is read and discarded first, so the client receives the `413` instead of a reset connection. A stalled body gets a `408` after a socket timeout. All errors, including protocol-level ones, are JSON.
- **Tests exercise the real thing.** The HTTP tests start the server on an OS-chosen free port and talk to it over a socket with `http.client` (which does not follow redirects). Time comes from an injected clock, so nothing depends on the current date or on sleeping.

## API

| Request | Result |
| --- | --- |
| `POST /shorten` with JSON `{"url": "...", "custom_code": "..."}` (`custom_code` optional, 3-32 of `A-Z a-z 0-9 - _`) | `201` with `code`, `short_url`, `url`; `400` invalid input; `409` custom code taken; `411`/`413`/`408` for body problems |
| `GET /<code>` | `302` with a `Location` header and one more click; `404` if unknown |
| `GET /stats/<code>` | `200` with `code`, `url`, `clicks`, `created_at` (UTC, ISO-8601); does not count a click; `404` if unknown |

The codes `shorten` and `stats` cannot be used as custom codes because they are service paths.

## Usage

```bash
python -m shorturl --db shorturl.db --port 8080
```

```bash
curl -s -X POST http://127.0.0.1:8080/shorten \
     -H "Content-Type: application/json" \
     -d '{"url": "https://example.com/some/long/path"}'
# {"code": "1001", "short_url": "http://127.0.0.1:8080/1001", "url": "https://example.com/some/long/path"}

curl -i http://127.0.0.1:8080/1001
# HTTP/1.0 302 Found
# Location: https://example.com/some/long/path

curl -s http://127.0.0.1:8080/stats/1001
# {"code": "1001", "url": "https://example.com/some/long/path", "clicks": 1, "created_at": "..."}
```

Use `--base-url https://sho.rt` when the service sits behind a public name: it is used for `short_url` and for the "links back to this service" check. From Python, `shorturl.create_server(db_path, host, port, base_url, clock)` returns a server you can run with `serve_forever()`.

## Running the tests

```bash
pip install pytest
python -m pytest -q
```

Developed and tested on Python 3.14 with the standard-library `sqlite3` (SQLite 3.35 or newer is needed for `UPDATE ... RETURNING`).

## Limitations

- `http.server` is not a production server: there is no TLS, authentication or rate limiting, and every request opens its own HTTP/1.0 connection. Put a real front end in front of it for anything public.
- The self-link check compares host names only. It ignores the port (so another local service on a different port is refused too), and it does not resolve DNS aliases or decimal/hex IP spellings. It cannot detect a redirect cycle that runs through a second shortener.
- URLs must be ASCII: internationalised host names need punycode and non-ASCII paths need percent-encoding.
- Submitting the same URL twice creates two separate links. Links cannot be edited, deleted or expired.
- Custom codes are case-sensitive (`abc` and `ABC` are different links). Every `GET /<code>` counts as a click, including crawlers, and `HEAD` is not supported.

Built with AI coding assistance.
