# Browser-Guided Workflow Discovery Design

## Goal

Extend authenticated crawling beyond links and JavaScript endpoint extraction so
the crawler can discover functionality hidden behind forms, buttons, conditional
UI, modals, and multi-step wizards.

The system combines three complementary capabilities:

1. an LLM-driven Playwright agent authenticates and operates stateful UI flows;
2. Katana autonomously expands the reachable HTTP and browser surface;
3. the orchestrator repeats both activities in one authenticated Chrome session
   until a bounded round finds nothing new.

The resulting sitemap is the controlled known-file and conservative Katana
baseline plus validated, additive evidence. Later stages never remove or
rewrite baseline entries.

## End-to-End Architecture

```text
POST /jobs
  |
  v
orchestrator starts one job-scoped Chrome + ephemeral profile
  |
  +--> CDP network guard attaches to every browser target
  |
  +--> Playwright authentication agent logs in
  |
  +--> controlled HTTP discovery fetches robots.txt and sitemap.xml
  |      |
  |      +--> known-file baseline evidence
  |
  +--> Katana pure-headless pass attaches to the same Chrome
  |      |
  |      +--> crawl baseline evidence
  |      +--> forms, XHRs, sources, and candidate-page index
  |
  +--> persist both evidence streams as the immutable baseline sitemap
  |
  +--> bounded discovery loop
         |
         +--> orchestrator selects candidate pages
         +--> Playwright/LLM explores safe stateful workflows
         +--> orchestrator records new states, requests, and stable URLs
         +--> Katana reattaches with stable URLs as additional seeds
         +--> stop when a complete round adds nothing or a budget expires
  |
  +--> validate and add discovery evidence to the baseline
  +--> persist result
  +--> close Chrome and destroy the profile
```

Chrome is started once per job and remains alive through authentication, the
baseline, every discovery round, and every Katana enrichment pass. Playwright,
the network guard, and Katana can use separate CDP WebSocket connections to the
same loopback endpoint. Only the orchestrator owns Chrome shutdown.

## Component Responsibilities

### Orchestrator

The orchestrator owns:

- the job state machine, deadlines, cancellation, and the single-job invariant;
- Chrome, CDP endpoint, ephemeral profile, and final cleanup;
- the controlled known-file fetcher and Katana subprocesses;
- authentication, crawl, discovery, processing, and publication transitions;
- candidate-page ranking and the coverage fixpoint;
- immutable baseline storage and additive evidence merging.

Neither Katana nor the model may start, stop, or configure another component.

### Job-scoped Chrome

Chrome starts with:

- a random loopback-only remote-debugging endpoint;
- a job-private temporary user-data directory;
- the default browser context, shared by authentication and discovery;
- downloads, permissions, service workers, and password storage disabled;
- no persistent credentials or profile data.

The default context preserves cookies, local storage, session storage, tabs, and
client-side wizard state for the complete job. Katana creates and closes its own
tabs but must not close Chrome or unrelated Playwright tabs.

### Authentication agent

Authentication uses the existing free-form LLM/tool loop:

- `get_page_state` exposes open pages, frames, visible text, and visible elements
  with stable references;
- the model uses bounded Playwright tools to navigate, type, select, and click;
- credential and TOTP tools exist only during authentication;
- successful authentication is verified before crawling;
- unsafe URLs observed during authentication become crawl exclusions.

The authenticated browser remains open. Structured exact-origin headers and a
cookie header are retained only for the controlled known-file fetcher.

### Katana

Katana is an autonomous, run-to-completion crawler. It does not pause for an LLM
decision and does not accept model-selected browser actions.

The orchestrator uses Katana's pure-headless engine. `-cwu` attaches Katana to
the shared Chrome for static and rendered DOM, JavaScript extraction, browser
actions, forms, and XHR discovery. Katana's standard HTTP engine is not run in
V1 because its requests would fall outside the CDP enforcement boundary.

Each pass:

- uses the authenticated default browser context;
- receives the target plus any stable additional seed URLs;
- emits JSONL and exits without terminating Chrome;
- contributes completed request, form, XHR, source, and navigation evidence.

### Discovery agent

Discovery reuses the authentication agent's page-state representation and
browser-tool interface. It receives a different objective and different
authority:

- discover new safe functionality rather than authenticate;
- no credentials, TOTP, headers, JavaScript execution, or CDP tools;
- no ability to grant request policy or control Katana;
- every browser tool call is checked against the live element and the active
  safe-action profile;
- observed browser state and network events, not model claims, prove success.

The agent completes or deliberately abandons a stateful workflow before Katana
is given the next enrichment checkpoint. It does not generate a multi-step
script for later replay.

### CDP network guard

A dedicated job-lifetime CDP client is the authoritative browser enforcement
and traffic-capture boundary:

- `Target.setAutoAttach` discovers every page, popup, frame, and worker target;
- `Fetch.enable` pauses matching requests before they are sent;
- the guard continues allowed requests and fails forbidden requests;
- `Network` events record request/response metadata and associate requests with
  the triggering action;
- new targets remain paused until interception is installed;
- guard failure cancels the active browser action or Katana pass and fails
  closed for network-producing discovery.

This guard covers pages opened by both Playwright and Katana. It retains only
the fields needed for policy, sitemap evidence, and causality. Secrets and
bodies are redacted before persistence.

## Network Enforcement and Capture

Chrome connects directly to targets. There is no proxy process or proxy
configuration in the runtime.

The enforcement and evidence boundaries are:

| Traffic | Enforcement and capture |
| --- | --- |
| Playwright authentication | CDP guard in authentication policy mode |
| Playwright discovery | CDP guard in discovery policy mode |
| Katana pure-headless traffic | The same CDP guard on Katana-created targets |
| `robots.txt` and `sitemap.xml` | Controlled async GET client owned by the orchestrator |

CDP events and Katana JSONL are merged by Chrome request ID where available and
otherwise by method, normalized URL, and bounded time window. The target's
server ledger remains the final safety oracle in controlled tests.

The CDP guard must be attached before the first navigation and before any new
target is resumed. If complete interception cannot be proven, active discovery
does not ship.

### Controlled known-file discovery

Katana's pure-headless engine does not run its standard known-files queue. The
orchestrator therefore performs two explicit, bounded requests:

- same-origin `GET /robots.txt`;
- same-origin `GET /sitemap.xml`.

The client receives only exact-origin auth headers/cookies, follows only
same-origin redirects, enforces response-size and time limits, and records
redacted request/response metadata. Parsed same-origin URLs become Katana seeds;
cross-origin or malformed entries are diagnostics only. This client is not a
general crawler and never submits forms or follows arbitrary links.

## Katana Configuration

Pin Katana `v1.6.1` by version and checksum. Every crawl round has one
pure-headless pass. It uses `-cwu` and must not be reported as hybrid because
`-cwu` takes precedence over `-hybrid`:

```text
-cwu <job-cdp-endpoint>
-no-incognito
-jsonl
-jc
-jsl
-fx
-xhr
-fs <field-scope>
-cs <crawl-scope>
-crawl-out-scope <exclusions>
-d <depth>
-pls heuristic
-mfc 10
-ct 5m
```

Katana v1.6.1 does not apply the common-engine `-rl`, `-hrl`, or `-mdp`
enforcement paths to the pure-headless engine selected by `-cwu`. The
orchestrator and CDP guard therefore enforce request rate, request count, page,
state, and duration budgets. A future Katana version may take over an individual
limit only after an integration test proves that the flag is enforced with
`-cwu`.

For sites with perpetual polling, WebSockets, or server-sent events, a separately
qualified fallback uses:

```text
-pls domcontentloaded -dwt 5
```

The following flags require separate comparison before becoming defaults:

- `-fsu -fst 10`: useful against path-value crawl traps, but capable of hiding
  value-dependent functionality;
- `-kb`: enabled only if its classification improves deterministic page ranking;
- `-aff -fc`: allowed only on controlled targets with an enforcing network
  policy because Katana chooses and submits forms autonomously.

Do not use Katana automatic login, CAPTCHA solving, or third-party solver keys.
The authentication agent supports richer login flows without placing plaintext
credentials in Katana arguments.

### Optional LLM-assisted Katana form filling

A capability experiment evaluates whether simple forms can be handled more
cheaply by Katana:

1. run Katana with `-fx` but without submission;
2. give the extracted, redacted form summary to the model;
3. let the model select synthetic value classes;
4. write a job-private temporary `-fc` file;
5. run a pure-headless `-aff -fc` pass under the CDP guard;
6. destroy the file immediately.

This is not the general wizard solution. Katana `v1.6.1` form configuration has
five global value classes—email, color, password, phone, and placeholder—not a
per-form action plan. It provides no model approval point before each submit and
cannot reason about conditional or multi-page workflows.

## Model Interface

The discovery model receives the same kind of page state used by authentication:

- current page and frame URLs, titles, and bounded visible text;
- visible inputs, selects, buttons, links, labels, roles, and options;
- stable element references;
- the discovery objective;
- a compact digest of known method/path templates and recent discoveries;
- executed actions and outcomes;
- remaining time, action, page, state, and request budgets.

It does not receive raw HTML, hidden DOM, cookies, authorization headers,
storage, request/response bodies, raw Katana JSONL, or persisted secrets.

The model may call the shared browser tools:

- `get_page_state`
- `switch_page`
- `click`
- `type_text`
- `select_option`
- `press`
- bounded same-origin navigation
- `finish_state`
- `finish_workflow`

Element references are required; raw selectors are rejected. Text input is
length- and character-bounded and may be model-proposed or generated from a
run-local synthetic intent. The model cannot override the execution or network
policy.

Page content is always marked as untrusted target data. Visible prompt injection
is shown as page content, never as operator or system instructions.

## Candidate-Page Selection

The system cannot know an undiscovered endpoint in advance. A "gap page" is a
ranked candidate, not a proven omission.

The candidate set begins with:

- the target URL;
- authenticated landing pages;
- successful same-origin HTML documents from Katana;
- stable same-origin GET pages discovered by browser actions.

When there are 25 or fewer eligible pages, discovery visits all of them. For a
larger site, the orchestrator deduplicates normalized page templates and ranks
pages using observed signals:

1. an extracted form whose action/method has no matching observed request;
2. an unrequested JavaScript endpoint, client route, or dynamic navigation;
3. visible interactive controls not yet exercised in the current state;
4. authenticated or newly discovered page templates;
5. a previous action on the page that added an endpoint or distinct UI state.

Unsafe paths, logout/login boundaries, non-HTML responses, duplicate templates,
exhausted states, and completed workflows are excluded. Stable URL order breaks
ranking ties so deterministic tests are reproducible.

## Discovery Loop and Fixpoint

One discovery round is:

1. choose the highest-ranked candidate page;
2. navigate or activate its existing tab in the shared context;
3. capture page state and fingerprint the normalized UI state;
4. let deterministic rules handle unambiguous UI-only actions;
5. ask the model to operate ambiguous forms, controls, or workflows;
6. record tool calls, state transitions, and causally related requests;
7. continue until the workflow is terminal, blocked, exhausted, or over budget;
8. collect newly stable same-origin GET seeds;
9. run one completed pure-headless Katana enrichment pass using those seeds;
10. update candidate ranking from the completed evidence.

A round expands coverage only when it adds a canonical endpoint, normalized UI
state, previously unseen actionable control, or completed workflow step. Tokens,
timestamps, polling events, random values, and repeated validation errors do not
count.

The loop stops when:

- a complete round adds nothing;
- all candidate states are terminal or exhausted;
- policy leaves no allowed action;
- the user cancels;
- any hard budget is reached.

## Safety Policy

The model may inspect every visible control, but it may execute only actions
allowed by a server-owned safe-action profile.

The default profile permits:

- expanding menus, tabs, accordions, dialogs, and drawers;
- scrolling and pagination;
- selecting filters and other non-mutating options;
- synthetic text entry without submission;
- same-origin GET navigation not classified as action-like.

Network-producing form submission requires an exact profile rule for origin,
method, normalized route pattern, action intent, and maximum invocation count.
Unknown POST, PUT, PATCH, and DELETE requests are blocked. GET requests with
logout, delete, unsubscribe, purchase, confirmation, or similar action semantics
are also blocked.

V1 always blocks:

- account creation and registration submission;
- logout, deletion, unsubscribe, deactivation, and permission changes;
- cart, checkout, payment, review, messaging, and contact submission;
- upload, download, clipboard, camera, location, notifications, and browser
  permissions;
- cross-origin active requests and top-level navigation;
- JavaScript URLs and non-HTTP schemes;
- WebSockets unless a later policy and capture design explicitly supports them.

## Evidence and Sitemap Contract

The controlled known-file and conservative Katana results are persisted
atomically as the immutable baseline before active discovery begins.

Discovery can add:

- canonical HTTP endpoints actually requested or observed through eligible
  browser XHR/form evidence;
- interactions that were observed but not executed;
- executed actions and normalized state transitions;
- workflow steps backed by browser or network evidence;
- blocked-attempt counters and stop reasons.

Discovery cannot remove, reorder, or alter baseline entries. Duplicates are
merged by canonical method, origin, normalized path, and query-key set without
changing the displayed baseline entry.

Persisted artifacts never contain credentials, cookies, authorization headers,
TOTP seeds, literal secret values, raw request bodies, raw response bodies, or
the ephemeral Chrome profile. Redaction occurs before bytes are written, not as
a later cleanup step.

## API and Job Lifecycle

Jobs continue to use `POST /jobs`, `GET /jobs/{job_id}`, and cancellation through
the crawler server. Discovery configuration is server-bounded:

```json
{
  "discovery": {
    "enabled": true,
    "mode": "active",
    "safe_action_profile": "default",
    "max_rounds": 3,
    "max_actions": 100,
    "max_candidate_pages": 25
  }
}
```

Clients may lower limits but cannot raise server caps or name arbitrary model,
browser, executable, environment variable, profile file, or API key.

The status sequence is:

```text
queued -> authenticating -> crawling -> processing_baseline
       -> discovering -> processing -> completed
```

Failure after the baseline produces a completed job with unchanged baseline or
validated partial additions. Cancellation stops the model, Katana, controlled
HTTP fetcher, CDP guard, and Chrome, then destroys credentials and the profile.

Default server budgets are:

- authentication: 5 minutes;
- each Katana pass: 5 minutes;
- guided discovery: 5 minutes total;
- enrichment passes: at most 3;
- total job deadline: 25 minutes;
- 500 Katana pages per pass;
- 25 candidate pages, 100 actions, 120 unique UI states, 40 model turns, and
  1,000 browser requests;
- 15 seconds of action settling time and 20 total redirects.

## Test Websites

All controlled websites must be healthy, resettable, and baselined before
discovery implementation begins.

### Repository fixtures

The complete 20-site roster is mandatory:

| Group | Sites | Purpose |
| --- | --- | --- |
| General | `site-a-static`, `site-b-login-flask`, `site-c-registration-express`, `site-d-complex-auth-go`, `site-e-crawl-trap-ruby`, `site-f-spa-deno` | Static crawling, authenticated application pages, registration safety, ambiguous actions, crawl traps, and SPA workflows. |
| Authentication | `auth-a-simple-form`, `auth-b-http-basic`, `auth-c-complex-form`, `auth-d-interactive-captcha`, `auth-e-delay-login`, `auth-f-ocr-captcha`, `auth-g-multi-step`, `auth-h-new-window`, `auth-i-iframe`, `auth-j-xsrf-token`, `auth-k-dynamic-fields`, `auth-l-security-question`, `auth-m-totp-mfa`, `auth-o-bearer-token` | Simple and complex login, headers, challenges, delayed UI, OCR, multi-step, popup, iframe, XSRF, dynamic fields, security questions, TOTP, and bearer auth. |

Three fixtures are authoritative for the new capability:

- `site-f-spa-deno`: hidden filter drawer, lazy table, modal, open Shadow DOM,
  and conditional report-builder wizard;
- `site-b-login-flask`: authenticated forms and a multi-step workflow whose
  terminal request is absent from ordinary links;
- `site-e-crawl-trap-ruby`: infinite navigation, duplicate states, background
  polling, prompt injection, misleading controls, and forbidden actions.

Each fixture manifest declares required baseline endpoints, browser-only
endpoints, allowed actions, forbidden routes, terminal workflow evidence, auth
reference, reset operation, and a server-side request ledger.

### External self-hosted targets

- [ZAP CrawlGround](https://github.com/zaproxy/crawlground), pinned to an audited
  commit, is the primary scored crawler obstacle course.
- OWASP Juice Shop provides a realistic JavaScript-heavy application and API.
- ParaBank provides authenticated multi-page application workflows.

These targets run locally in pinned containers and are reset before every mode.

### Restricted public canaries

UI Testing Playground, the nopCommerce demo, and Automation Exercise are
non-blocking reachability canaries only. Public runs prohibit authentication,
form submission, carts, accounts, checkout, messaging, contact, reviews, and
other mutations. They are never release gates.

## Test Procedure

End-to-end tests use the deployed crawler boundary, not an in-process harness:

1. start every external target;
2. start `uv run tenzai-crawler-server` with temporary DB and log paths;
3. wait for server readiness;
4. reset each target;
5. submit a job through `POST /jobs` or `tenzai-crawler create`;
6. poll `GET /jobs/{job_id}` until terminal;
7. retrieve and analyze the returned sitemap and target request ledger;
8. restart the server and verify persisted job retrieval;
9. stop the server and targets.

This exercises Uvicorn lifespan, serialization, validation, API timeouts, CLI/API
compatibility, sequential queue handling, persistence, cross-job isolation, and
real subprocess cleanup.

The recorded starting measurement is:

- 23/23 controlled targets completed through the real server;
- 310 sitemap entries;
- 0/10 declared browser-only fixture controls reached;
- CrawlGround 1/59;
- 23/23 results retrieved after restart;
- zero cross-job isolation violations and zero blocked-route hits.

## Capability Matrix

Before product implementation, score these reset modes independently:

1. controlled known-file discovery with public, authenticated, redirected,
   oversized, malformed, and cross-origin responses;
2. Katana `v1.6.1` pure headless with `-fx`, `-xhr`, JavaScript extraction, and
   the qualified load strategy;
3. Katana automatic fill with a synthetic `-fc` file on controlled targets;
4. scripted Playwright reachability proving every required and forbidden route;
5. scripted-model discovery using the real shared browser and CDP guard;
6. live-model discovery, repeated three times;
7. stable-seed pure-headless enrichment after browser discovery.

For every mode record endpoint coverage, CrawlGround score, UI states, workflow
terminals, elapsed time, requests, blocked attempts, forbidden server hits, and
whether Chrome/profile state survived each pass boundary.

## Tests Where Discovery Can Make Results Worse

The suite must contain negative controls for:

- automatic form filling submitting registration, logout, deletion, or another
  mutation;
- the model following visible or hidden prompt injection;
- a wrong synthetic value hiding the branch that a valid value reveals;
- `-fsu` collapsing URLs whose different values expose different behavior;
- infinite calendars, cursors, pagination, consent loops, or repeated modals;
- background polling and volatile DOM values preventing a fixpoint;
- Katana closing or changing a Playwright wizard tab;
- a Katana-created tab issuing a request before CDP interception is installed;
- Katana JSONL and CDP events producing duplicates or contradictory evidence;
- known-file redirects escaping origin policy or receiving incorrect auth
  headers;
- replacing the previous Katana engine path with pure headless plus controlled
  known-file fetching and losing an endpoint from the recorded baseline;
- service workers, popups, redirects, downloads, WebSockets, or cross-origin
  requests bypassing normal evidence capture;
- stale element references, model hallucinations, invalid tool arguments, and
  repeated ineffective actions;
- higher latency and model cost without any coverage improvement;
- failure, timeout, or cancellation leaving Chrome, Katana, profile data, the
  known-file fetcher, or request interception alive;
- state or evidence leaking from one sequential job into the next.

Each negative test compares the final sitemap with the immutable baseline and
the server ledger. A mode is unsafe if the server receives one forbidden request,
even when the request is absent from the sitemap.

## Acceptance Criteria

Implementation starts only after:

- all 20 repository fixtures, CrawlGround, Juice Shop, and ParaBank are pinned,
  healthy, resettable, manifested, and reproducibly baselined;
- the Katana capability matrix establishes the explicit production flags;
- shared Chrome preserves authentication and wizard state across every pass;
- Katana disconnects without closing Chrome;
- the CDP guard intercepts the first request from every Katana-created target;
- controlled known-file discovery remains same-origin, bounded, authenticated
  where required, and reproducible;
- forbidden server hits remain zero in all safe modes.

V1 is complete when:

- discovery-disabled output is byte-for-byte compatible with the baseline;
- baseline endpoints remain an immutable subset of the final sitemap;
- scripted-model discovery reaches every declared in-scope browser-only fixture
  endpoint and terminal workflow;
- CrawlGround coverage improves beyond active stock Katana without losing a
  previously reached marker;
- malformed model calls, browser/Katana failures, and timeouts preserve the
  baseline;
- cancellation cleans up all processes and browser state promptly;
- persisted output and logs pass secret/redaction checks;
- runtime and E2E tooling have no Proxify process or binary dependency;
- a final complete discovery round produces no new endpoint, state, control, or
  workflow evidence.

## Delivery Phases

### Phase 0 — Test environment and baseline

- bring up all 23 controlled targets;
- implement health, reset, version, and manifest checks;
- run the real-server baseline procedure;
- publish the machine-readable and Markdown baseline reports.

Delivery: one end-to-end report showing current crawler results for every target.

### Phase 0.5 — Katana, Chrome, and network capability gate

- pin and checksum Katana `v1.6.1`;
- run the capability matrix;
- prove shared default-context state and pass-level attach/detach;
- prove the CDP guard covers Katana-created targets;
- prove the controlled known-file fetcher and pure-headless pass retain the
  required conservative coverage without Katana's standard engine;
- choose the load strategy and optional `-fsu`/`-kb` behavior;
- decide whether LLM-assisted `-aff` provides safe value.

Delivery: capability report plus the final production flag and network-boundary
decision.

### Phase 1 — Browser lifecycle and immutable baseline

- add the orchestrator-owned Chrome and CDP guard;
- add controlled known-file discovery;
- reuse the default context for authentication;
- attach Katana to the shared Chrome;
- parse and merge only controlled-fetch evidence, Katana JSONL, and CDP events;
- remove the Proxify runtime process, binary dependency, configuration, log
  parser, and shutdown path;
- persist the conservative baseline atomically;
- implement cancellation and cleanup.

### Phase 2 — Discovery agent and fixpoint

- extract the shared authentication/discovery browser controller;
- add candidate-page indexing and ranking;
- add discovery policy and tool enforcement;
- implement browser evidence, Katana enrichment, deduplication, and fixpoint;
- add additive sitemap and workflow evidence merging.

### Phase 3 — Qualification

- run deterministic scripted-model tests on every controlled target;
- run the CrawlGround comparison;
- run three repeated live-model qualifications on authoritative fixtures;
- document limitations and keep public canaries non-blocking.

## Verification

```bash
uv run pytest -q
uv run ruff check app tests scripts
uv run ty check
```

E2E verification additionally requires `RUN_E2E=1`, Katana in `PATH`, all
controlled targets running, and the crawler server started with temporary
DB/log paths.
