# web

## Scope

Static browser UI served by `liot.demo_server` (no build step, plain JS):

- `app.html` + `assets/app.js` + `assets/app.css` — the pipeline lab: board of sensors/devices from `GET /api/catalog`, presets, registration forms, `POST /api/simulate` trace view (trigger, two lines, embedding, arbiter, Gate1, Gate2, judgment with severity and sources, proposal, intervention), phone-feedback simulation (`/api/feedback`), directive chat (`/api/directive`).
- `safety.html` + `assets/safety.js` — home and guardian settings: home alias and lives-alone, residents and their yes/no answers with "who is entering", guardian registration (secret topic shown once), consent grant/revoke by a chosen resident, consent history, forwarding history. Talks only to `/api/safety/*`.
- `home3d.html` + `assets/home3d.js` + `assets/home3d.css` — 3D virtual home (three.js, hand-written pointer rotation, 2D floor-plan fallback when WebGL or the module fails) with resident and guardian phones and a playback timeline. Talks only to `GET /api/catalog` and `POST /api/home3d/scenario`; every title, body, priority, button label and time it shows comes from that response.
- `day.html` + `assets/day.js` + `assets/day.css` — simulated days on the real `LiotService` (`/api/day/*`): day tabs, virtual clock and weather line, a vertical timeline of server `line_ko` lines, the resident phone (red safety card on top with the real ntfy title/body from the send record, proposal cards with [도움됐어요]·[무시]·[그만해] and the server `state_ko`, a 모아보기 tray for DIGEST items), per-day metrics, a 3-day comparison table and '속 보기' (rule, matched/skipped rules, score vs NoOp, candidates, tier, gate decision). Streams with `EventSource` from `lastSeq + 1` and reconnects from there; viewer taps POST `/api/day/feedback` and show "다음 단계에 반영돼요" until the `feedback` event arrives; [무시] sends nothing. Tokens are a copy of the home3d `:root` block.
- `assets/vendor/three.module.min.js` — three.js 0.160.0, pinned by sha256 in `assets/vendor/README.md`, MIT (`three.LICENSE`). Replace only with a recorded version and hash; never load three.js from a CDN.
- `index.html` + `assets/main.js` + `assets/style.css` + `assets/presets.json` — older landing page and preset data.

Not this module's scope: any decision logic (the server computes everything), persistence, authentication, notifications.

## Boundaries

- The UI never computes tiers, severities, or eligibility itself; it renders what the API returns.
- `safety.js` must not persist resident answers or guardian topics anywhere (`localStorage`, `sessionStorage`, cookies, query strings, non-local URLs). Answers live only in the in-memory `residents` list for rendering. The topic exists only as a local variable and DOM node inside `showTopicOnce` and disappears when that dialog closes.
- No external network loads from `app.html`, `safety.html` or `home3d.html` (local assets and a data: icon only; only the older `index.html` loads Google Fonts). Do not add other-origin scripts or styles to the safety page.
- Requests to `/api/safety/*` are same-origin `fetch` with `Content-Type: application/json`; the server rejects anything else.

## Invariants

- The safety page has no input for birth date or diagnosis; the three resident questions are exactly "75세 이상인가요?", "혼자 대피할 수 있나요?" (stored inverted as `cannot_self_evacuate`), "인지 저하가 있나요? (보호자만 입력)". Question text comes from the server response (`answers[key].question`).
- `cognitive_decline` is only submittable with `entered_by: guardian`; the UI marks it guardian-only (the server enforces it with 403).
- The consent notice states the answers stay on this device and are used only to decide how strongly to alert.
- Severity is displayed with its Korean label and its sources (표·거주자·모델); a judgment without severity shows "없음" in the 파급도 row and "—" for sources. The lab currently always shows "없음" because the lab path does not run situation logic.
- Error messages shown to the user are the server's one-line Korean `error` string.

## Patterns

- `getJson` / `postJson` helpers wrap `fetch(..., {cache: "no-store"})`; every response is `{ok, ...}` and the UI checks `data.ok` before using fields.
- DOM is built with the `el(tag, cls, text)` helper using `textContent` (never `innerHTML` with server data).
- JSDoc types on functions.

## Tests

- No browser automation exists in the test suite. `tests/test_day_api.py` checks that `day.html` and its assets are served, load nothing from other origins and never use `innerHTML`; `tests/test_review_fixes.py` pins the header links to `day.html` on every page. `tests/test_demo_server_safety.py` checks the safety page is served and contains no birthdate/diagnosis fields; `tests/test_demo_flow.py` and `tests/test_demo_server_*.py` cover the APIs the UI calls.
- After a UI change, open `http://127.0.0.1:8765/` and `/safety.html` in a browser on this machine and exercise: add resident, answer as resident (cognitive decline must be refused), add guardian (topic shown once), grant and revoke consent from two different residents, view histories.
