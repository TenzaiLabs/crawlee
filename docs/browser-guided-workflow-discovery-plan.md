# E2E-First Browser-Guided Discovery Design

## Summary

This document is the implementation source of truth for browser-guided workflow
discovery.

- Discovery is enabled by default; clients may disable it or lower server-owned
  limits.
- Proxify is removed completely, including legacy job and log compatibility.
- The public sitemap `entries` and `tree` shapes remain stable, but historical
  byte-for-byte output is not a compatibility requirement.
- The Step 1 real-server baseline is 527 entries, 0/10 browser-only controls,
  5/10 lane-specific capability markers, and CrawlGround 3/59.
- The Step 2 direct-Katana checkpoint is 304 entries, 0/10 browser-only
  controls, 5/10 lane-specific capability markers, and CrawlGround 3/59.
  All 24 jobs completed, survived restart retrieval, and had zero cross-job
  isolation violations.
- The Step 3 disposable-browser spike passes 24/24 capability checks against
  the exact Katana binary shipped in the crawler image. It proves the shared
  Chrome handoff, passive CDP coverage, both Katana lanes, authentication-header
  propagation, and per-input terminal reasons.
- Every crawl stage deliberately runs both Katana engines: the standard HTTP
  engine for static/JavaScript analysis and the pure-headless `-cwu` engine for
  rendered browser discovery.
- Katana owns crawl scope, URL uniqueness, similar-URL filtering, redirects,
  page classification, and crawl expansion within each pass. The orchestrator
  does not duplicate those decisions.
- Real-server E2E results are authoritative. Existing tests remain only when
  they validate a still-relevant contract.
- Final qualification completes all 21/21 repository-controlled fixtures,
  finds 10/10 B/E/F browser-only endpoints, 5/5 required request sequences,
  and 11/11 lane-specific markers, verifies all 24 persisted jobs after
  restart, and records zero cross-job isolation violations. CrawlGround and
  ParaBank remain explicit non-blocking public-canary failures because their
  pure-headless queues do not exhaust within ten minutes.

```text
Playwright authentication in job Chrome
        |
        +--> exported cookies/headers --> standard Katana
        |                                  (-jc, -jsl, -fx, -kb, -td)
        |
        +--> close Playwright pages --> pure-headless Katana on same Chrome
                                           + passive CDP XHR/fetch evidence
        |
        v
merge exact public endpoints + retain per-lane evidence
        |
        v
Playwright/LLM opens fresh pages, explores gaps, and returns stable GET seeds
        |
        +--> repeat the two Katana passes until fixpoint or budget
```

## Target Design and Interfaces

### Runtime module boundaries

The implementation keeps model decisions separate from trusted browser and
orchestration behavior:

- `discovery_model.py` owns live LLM prompting, structured-response parsing,
  semantic validation, and model-turn accounting;
- `browser_discovery.py` owns typed page/frame state, Playwright capture and
  action execution, candidate/evidence policy, and one bounded discovery round;
- `orchestrator.py` owns browser epochs and pipeline coordination through one
  typed Katana lane runner plus a distinct baseline phase;
- `job_persistence.py` owns compare-and-set job transitions, baseline and
  partial publication, latest-checkpoint finalization, and atomic completion.

This preserves the central trust boundary: the model proposes one action, but
the browser runtime captures state, validates and executes the action, and
decides what counts as verified evidence. DOM capture and action execution may
move into a separate Playwright driver if that module grows further; they are
kept together for now because they share element-reference and frame state.

### Shared browser lifecycle

The orchestrator launches one job-scoped Chromium process with an ephemeral
profile and loopback CDP endpoint. Playwright authentication, Katana `-cwu`,
and Playwright discovery reuse that browser profile sequentially. The standard
Katana pass is an HTTP crawl and does not use Chromium.

The orchestrator:

- starts the Playwright-bundled Chromium executable with a job-private
  `--user-data-dir`, loopback-only remote debugging, and an OS-assigned port;
- reads the DevTools WebSocket endpoint from the job profile's
  `DevToolsActivePort` file with a startup timeout;
- connects Playwright and the passive CDP observer before the first navigation;
- gives Katana the same endpoint through `-cwu`;
- owns cancellation, shutdown, subprocess cleanup, and profile deletion.

Playwright and Katana never drive the browser concurrently. Before each
pure-headless pass, the orchestrator closes every Playwright page and stops
driving the browser. Katana then has an exclusive browser epoch and may manage
all page targets. After Katana disconnects, Playwright reconnects and opens new
pages when guided discovery is required. Cookies and origin `localStorage`
survive through the shared profile; the design does not preserve tabs,
`sessionStorage`, an open DOM, or partially completed wizard state across a
Katana boundary.

Browser or observer disconnect immediately cancels the active pass; the
orchestrator does not launch a replacement Chrome and claim to have recovered
the session. Before the baseline checkpoint this fails the job. After the
checkpoint it finalizes validated evidence with `partial_failure` or
`interrupted`. Forced termination kills the Katana process group, enumerates and
closes any leftover page targets, and only then allows Playwright to reconnect.
Final cleanup always stops Katana, Chrome, CDP sessions, and the job profile.

### CDP observation and authentication propagation

A raw CDP observer attaches before navigation. It enables `Network` events on
pages, popups, frames, workers, and service workers so browser traffic can be
associated with the active Katana pass or LLM action.

The observer:

- records request and response metadata without blocking, rewriting, or
  cancelling requests;
- associates requests with actions through CDP request, loader, frame,
  initiator, and action identifiers;
- reports observation gaps as diagnostics rather than acting as a second crawl
  policy engine.

Auto-attachment never pauses a target for debugger setup. `Network.enable`
therefore has an unavoidable race with the first events from a newly created
page, iframe, or worker. The observer emits an
`initial-network-observation-gap` diagnostic for each attached session (up to a
bounded diagnostic limit) instead of delaying target execution to close that
gap.

Cookies remain in the shared Chrome context. Structured cookies and headers are
retained in memory for browser, both Katana passes, and controlled known-file
use. Cookie domain, path, Secure, and SameSite attributes determine which
subdomains receive cookies. Non-cookie authentication headers apply to every
host admitted by the configured Katana crawl scope, including matching
subdomains. The standard pass receives them with `-H`. Stock `v1.6.1`
pure-headless ignores `-H`, so the project uses a source-pinned
`v1.6.1`-derived build that passes custom headers into browser requests admitted
by Katana's configured crawl scope. This avoids turning the observer into a
request-interception mechanism.

URL and time-window matching cannot prove that an action caused a request. Every
controlled-target ledger entry must match either CDP evidence or
controlled-fetch evidence. An unexplained browser request is an observation
defect to investigate before qualification.

### Katana trust boundary

Katana is trusted to apply its own scope, uniqueness, similar-URL filtering,
redirect behavior, and browser actions. The orchestrator does not block HTTP
methods, destructive-looking routes, or Katana requests. It also does not impose
a second rate limiter, URL deduplicator, or page-count limit.

For pure-headless, Katana scope governs action admission and emitted results;
it is not a pre-egress boundary for every redirect or browser subresource.
Katana itself uses CDP response interception and its built-in browser behavior.
“Passive CDP” in this design refers only to the orchestrator's evidence
observer.

The LLM receives a conservative discovery objective, but its browser requests
are not intercepted or blocked. Controlled fixtures record destructive markers
in their server ledgers so E2E qualification can verify the behavior rather than
changing it at runtime.

### Authentication handoff

The authentication agent consumes the orchestrator-owned context and controller
instead of launching and closing its own browser. It retains the existing
free-form page-state and browser-tool loop and returns:

- structured cookies;
- crawl-scope header material;
- authenticated landing URL;
- discovered seed URLs.

Credentialed authentication is accepted only through deterministic browser
evidence. Browser route identity preserves fragments, so hash-router states such
as `#/reports` are navigated and evaluated rather than collapsed into `/`.
Verification opens a temporary clean context in the same Chrome instance and
compares it with the authenticated context. A configured success indicator is
authoritative; otherwise the gate requires a protected network-status delta, a
clean-context login/denial redirect, or clean login controls replaced by
authenticated controls. Cookies, storage, a changed URL, or a public HTTP 200
are supporting evidence only and cannot independently pass the gate. The clean
context is opened only for verification and then closed.

Credential and TOTP tools exist only during authentication. Manual-header mode
does not invoke AI authentication; its headers are applied to browser and Katana
requests for hosts admitted by the configured crawl scope.

### Controlled known-file discovery

Katana `v1.6.1` supports `-kf` in the standard and hybrid engines, but
`-cwu` selects the pure-headless engine. That engine does not use
`common.Shared.NewCrawlSessionWithURL`, which is where
`KnownFiles.Request` queues `robots.txt` and sitemap requests. This is a
verified `v1.6.1` pure-headless wiring gap, not a general Katana limitation.
The native standard-engine implementation also does not provide the required
global 100-document budget or a 5-MiB decoded-document limit.

The orchestrator therefore performs bounded async GET requests for:

- in-scope `/robots.txt`;
- in-scope `/sitemap.xml`;
- in-scope sitemap URLs declared by `Sitemap:` lines;
- in-scope sitemap documents referenced by sitemap indexes.

Discovery starts with the target origin. When either Katana pass emits a new
in-scope subdomain origin, that origin's `robots.txt` and `sitemap.xml` become
eligible under the same global document budget. Because a running Katana
process cannot accept more seeds, documents learned during a stage are fetched
and supplied to the next stage; they are not claimed as part of the already
running pass.

The fetcher follows only in-scope redirects, applies crawl-scope
authentication, and never submits forms or follows general page links. Defaults
are:

- 100 known-file documents;
- 5 MiB of decoded content per response;
- 10 seconds per request;
- no separate seed-count cap: every valid in-scope URL from the accepted
  documents is passed to Katana.

Redirects, sitemap indexes, and documents each consume one of the 100 attempts;
cycles are detected by exact document URL. Malformed, oversized, cyclic, and
out-of-scope declarations are recorded as diagnostics and are not followed.

### Katana baseline and enrichment

The project Katana build remains pinned by base version (`v1.6.1`), patch
commit, and binary checksum. Its project-specific changes are intentionally
limited to pure-headless header propagation and machine-readable terminal
summaries. Every baseline or enrichment stage has two sequential Katana passes
over closed, related seed batches.

The standard HTTP pass runs first. It receives the target, authenticated landing
page, controlled known-file URLs, and stable GET seeds produced by guided
discovery:

```text
-jsonl
-duc
-jc
-jsl
-fx
-kb
-td
-fsu
-fst 10
-fs <field-scope>
-cs <crawl-scope>
-d <depth>
-mrs 5242880
-ct 10m
-H <header>...
```

The pure-headless pass then uses the authenticated shared browser. It receives
the target, authenticated landing page, and stable browser GET seeds. Standard
Katana output is merged into the baseline and candidate index, but every
standard response is not automatically turned into a pure-headless root seed;
doing so would repeat browser action trees and defeat serial `-p 1` execution.

```text
-cwu <job-cdp-endpoint>
-jsonl
-duc
-kb
-fsu
-fst 10
-fs <field-scope>
-cs <crawl-scope>
-d <depth>
-p 1
-xhr
-mfc 0
-pls domcontentloaded
-dwt 2
-ct 10m
-H <header>...
```

`-hybrid` is not used: the CLI cannot combine it with `-cwu`; `-cwu` always
selects pure-headless. The split is mandatory for stock `v1.6.1` and is retained
in the project build rather than patching all extraction features into the
browser engine. The capability matrix is:

| Capability | Standard HTTP pass | Pure-headless `-cwu` pass | Design decision |
| --- | --- | --- | --- |
| `-jc` | Configures Katana's regex JavaScript parsers. | Accepted but ignored because pure-headless creates an unconfigured response parser. | Enable only on standard. |
| `-jsl` | Configures Katana's jsluice parsers. | Accepted but ignored for the same reason. | Enable only on standard. |
| `-fx` | Extracts HTML forms from parsed responses. | Accepted but not wired. | Enable on standard; rendered controls also come from Playwright page state and `-kb`. |
| `-xhr` | No browser traffic exists to observe. | The pinned build emits browser XHR/fetch discoveries; the site-g real-server run qualified it. | Enable on pure-headless and retain passive CDP as the independent response/initiator evidence lane. |
| `-H` | Applied by the standard HTTP client. | Ignored by stock `v1.6.1`; the pinned project build passes it into scoped browser requests. | Enable on both and qualify same-host and matching-subdomain behavior. |
| `-kb` | Emits `knowledgebase.PageType` and form classifications. | Emits the same classification from browser response bodies. | Enable on both. |
| `-td` | Emits technology detection. | Initialized but not emitted. | Enable only on standard. |
| `-fpt` | Filters output using knowledge-base page type. | Filters output using knowledge-base page type. | Leave off initially so candidate selection sees every classified page. |
| `-fsu -fst 10` | Applies Katana similar-URL filtering. | Affects emitted/parser-discovered URL uniqueness, not browser-action scheduling. | Enable and trust each pass without claiming reduced browser traffic. |
| `-iqp` | Removes query values from the uniqueness key. | Removes query values from the uniqueness key. | Leave off because values can expose distinct functionality. |
| `-kf` | Works, but lacks the required aggregate document limits. | Not wired. | Replace with controlled known-file discovery. |
| `-mrs` | Bounds response bodies read by the standard crawler. | Not passed into the pure-headless crawler. | Set 5 MiB on standard; enforce a job-level memory ceiling for browser responses. |
| `-pls`, `-dwt` | Not applicable. | Configure browser page loading. | Use the qualified strategy on pure-headless. |
| `-d` | Limits queued request depth. | Limits browser action depth, not subresources or parser discoveries. | Enable with engine-specific expectations. |
| `-dr` | Disables standard-client redirects. | Ignored; Chrome follows redirects. | Leave off and retain redirect evidence when observed. |
| `-ct` | Per-input-seed deadline. | Per-input-seed deadline. | Keep as a seed failsafe; the orchestrator owns each process deadline. |
| `-p` | Controls concurrent input seeds. | Multiple seeds otherwise drive and close targets concurrently in one shared browser. | Force `-p 1` on pure-headless. |
| `-no-incognito` | Applies only when Katana launches Chrome. | Has no effect with `-cwu`. | Omit. The orchestrator owns the attached profile. |
| `-duc` | Disables update checks. | Disables update checks. | Enable on both for the pinned build. |
| `-pc`, `-s` | Work in the common standard loop. | Not passed through or not honored as claimed. | Do not base the design on them. |
| `-rl`, `-hrl`, `-mdp` | Enforced. | Not enforced in the `-cwu` loop. | Do not claim them for browser traffic. |

The CDP collector is passive: it listens to browser network events and emits
method, URL, resource type, initiator, and action attribution independently of
Katana's qualified pure-headless `-xhr` output. It does not intercept, pause,
rewrite, or decide whether a request may run. Both sources retain separate
provenance; CDP additionally supplies response, worker, frame, popup, and
initiator attribution.

The pinned build writes an atomic machine-readable process summary containing a
terminal record for every input seed. Schema version 1 has process status
`completed` or `cancelled`; per-input reasons are `queue_exhausted`,
`crawl_timeout`, `input_failure`, or `cancelled`, with an optional error string.
Every `queue_exhausted` seed makes the lane complete. A `crawl_timeout` or
`input_failure` makes the lane partial when the process exited normally, the
terminal summary is valid, and the JSONL contains usable sitemap evidence. The
orchestrator parses, merges, and checkpoints that evidence instead of discarding
it. Missing or corrupt summaries, unknown terminal reasons, process failures,
and partial lanes with no usable sitemap evidence remain failures. Katana JSONL
and process exit status alone are insufficient to classify completeness.

The controlled known-file fetcher and standard Katana pass use 5 MiB response
limits. Katana's default unique filter remains enabled independently in each
process. The orchestrator does not add another crawl-time URL deduplication or
similarity algorithm.

For sites with perpetual polling, WebSockets, or server-sent events, a
separately qualified fallback uses:

```text
-pls domcontentloaded -dwt 5
```

In `v1.6.1`, that strategy waits for the page `load` event and then sleeps for
five seconds; its name does not mean a literal DOMContentLoaded-only wait.

Katana `-aff/-fc` remains an isolated experiment and is not a V1 dependency.

### Discovery model interface

The discovery model receives:

- current page and frame URLs, titles, and bounded visible text;
- visible inputs, selects, buttons, links, labels, roles, and options;
- stable element references;
- the current discovery objective;
- known method/path templates and recent discoveries;
- executed actions and verified outcomes;
- remaining time, action, page, state, and model-turn budgets.

It does not receive raw HTML, hidden DOM, cookies, authorization headers,
storage, raw request or response bodies, raw Katana JSONL, or persisted secrets.

The implemented interface reuses the authentication flow's stable element refs
and action vocabulary but keeps page capture and execution in the orchestrator.
For one current state, the model returns exactly one strict decision:
`click`, `fill`, `select`, `press`, or `finish`, plus a current ref and value
where required. The runtime validates the ref and control type, executes the
action, settles the page, handles a newly opened tab, recaptures state, and
records the verified outcome before the next model call. Candidate navigation,
including matching subdomains, remains orchestrator-owned.

The model cannot control Katana or claim evidence. Page content is always
untrusted target data.

### Candidate selection and discovery rounds

Candidates begin with the target URL, authenticated landing pages, every
successful in-scope HTML document emitted by Katana, and stable in-scope GET
pages discovered through browser actions.

Enrichment seeds are not derived from the full round traffic window. Candidate
`goto` requests, subresources, XHR/fetch endpoints, and URLs already present as
known GET endpoints remain evidence but do not trigger Katana. Only a new
successful in-scope CDP `Document` request within an action window, or the new
stable page URL observed after that action, becomes an enrichment seed.

The orchestrator does not deduplicate, collapse, or template-normalize Katana
pages. Katana's unique filter and `-fsu` result are authoritative. The
orchestrator records only whether an exact candidate URL and UI state have
already been handed to the LLM so it does not repeat completed work.

Each Katana process has its own unique filter, so the same endpoint may be
emitted by the standard and pure-headless passes. Immutable evidence records
retain their pass provenance. Building the stable public sitemap performs only
the existing exact `(method, URL)` aggregation; this is result assembly, not a
second crawler uniqueness or similar-URL policy.

Candidates are ranked using Katana output by:

1. Katana knowledge-base page type classification;
2. an extracted form whose action has no matching request;
3. an unrequested JavaScript endpoint, client route, or dynamic navigation;
4. visible interactive controls not exercised in the current state;
5. authenticated or newly discovered pages;
6. a prior action that added an endpoint or distinct UI state.

Non-HTML responses, exact states already processed by the LLM, exhausted states,
and completed workflows are excluded from LLM work. They are not removed from
the Katana sitemap.

One discovery round is a bounded sweep:

1. snapshot and rank all eligible candidates;
2. visit each candidate once within the remaining page and action budgets;
3. fingerprint its normalized UI state;
4. apply deterministic UI actions when unambiguous;
5. use the model for ambiguous controls, forms, and workflows;
6. record state transitions and CDP-attributed evidence;
7. collect new, action-derived, stable, in-scope document GET seeds;
8. run one completed two-pass Katana enrichment stage with all new seeds;
9. rebuild and rank the candidate set.

The loop stops when a full sweep and enrichment stage add no endpoint, state,
actionable control, or workflow step; all candidates are exhausted;
cancellation occurs; or a hard budget is reached.

### API contract

`POST /jobs` accepts an optional `discovery` field. Omission enables
discovery with server defaults:

```json
{
  "discovery": {
    "enabled": true,
    "max_rounds": 3,
    "max_actions": 100,
    "max_llm_pages": 25
  }
}
```

Clients may disable discovery or lower limits but cannot exceed server caps or
select arbitrary models,
executables, files, environment variables, or API keys. The validated discovery
configuration is persisted and returned in job responses.

The public lifecycle adds only `discovering`:

```text
queued -> authenticating? -> crawling -> discovering? -> processing -> completed
```

### Persistence and result contract

The controlled known-file and dual-pass Katana results are atomically stored as
`baseline_sitemap` before discovery starts. Final sitemap entries are an
additive exact `(method, URL)` merge over this checkpoint. Raw Katana, CDP, and
controlled-fetch evidence remains immutable and retains pass and action
provenance even when multiple records map to one public endpoint.

The standard lane publishes an intermediate baseline checkpoint before the
pure-headless lane starts. A normally terminated budget-limited lane contributes
its valid JSONL and produces a completed result with
`result_metadata.completeness = "partial"` plus lane warnings. A partial lane can
never establish a discovery fixpoint; the discovery outcome remains
`budget_exhausted` until a later complete run proves exhaustion.

The final sitemap retains `entries` and `tree` and adds:

```json
{
  "discovery": {
    "outcome": "fixpoint",
    "rounds": 2,
    "new_entry_count": 7,
    "state_count": 12,
    "workflow_count": 2,
    "stop_reason": "complete_round_added_nothing"
  }
}
```

Allowed outcomes are:

- `fixpoint`;
- `disabled`;
- `budget_exhausted`;
- `partial_failure`;
- `interrupted`.

`workflow_count` is runtime-derived, not model-reported. It counts candidate
interaction sequences in which at least one executed action produced a
verified UI-state transition or a successful in-scope Document, XHR, or fetch
request. Multiple effective steps on the same candidate count as one workflow.

A failure after baseline persistence completes the job from the checkpoint plus
validated partial additions and records a partial outcome. On server startup,
an interrupted discovery job with a valid baseline checkpoint is finalized from
that checkpoint with outcome `interrupted`. Jobs interrupted before a
checkpoint remain `failed_interrupted`.

Operator cancellation retains job status `cancelled`. If a valid baseline or
partial discovery checkpoint exists, the newest valid checkpoint is published
with outcome `interrupted` and stop reason `cancelled_after_checkpoint`, and
`GET /jobs/{job_id}` returns that sitemap. Cancellation before the baseline has
no sitemap. A server task interruption without an operator cancellation leaves
the active status for startup recovery instead of converting it to an operator
cancellation.

There is no migration, fallback parser, backfill, or compatibility behavior for
old database rows or Proxify-only logs.

There is no general content or evidence redaction. The repository guardrail still
requires that plaintext credentials, cookie and authorization values, TOTP
seeds, and literal secret values never be persisted. The ephemeral Chrome
profile is also never persisted as a job artifact. All other Katana, CDP, and
workflow evidence may be retained as emitted.

### Budgets

Defaults are:

- authentication: 5 minutes;
- initial standard Katana pass: a 10-minute Katana crawl budget with a 12-minute
  process deadline for terminal-summary and shutdown completion;
- initial pure-headless Katana pass: the same 10-minute crawl and 12-minute
  process deadlines;
- guided browser actions: 5 minutes total;
- enrichment: at most 3 two-pass stages, with the same 10-minute crawl and
  12-minute process deadlines per Katana process;
- total job deadline: 60 minutes;
- 25 pages operated by the LLM;
- 100 actions;
- 120 unique UI states;
- 40 model turns;
- 15 seconds of action settling time.

Katana has no orchestrator-imposed page-count or request-count cap. Katana owns
URL uniqueness and similar-URL filtering within each process. `-ct 10m` is a
per-input-seed Katana timeout, not a whole-process deadline, so the orchestrator
also enforces a 12-minute wall-clock deadline for each Katana process and records
deadline termination as `budget_exhausted`, never as a fixpoint. Only the
source-pinned Katana terminal summary may establish queue exhaustion; process
exit 0 alone cannot. The pure-headless pass sets `-mfc 0` for unlimited
consecutive failures; the terminal summary, rather than that counter,
establishes completion.

The job Chrome and every active Katana process group share a 2-GiB RSS ceiling.
Exceeding it yields `partial_failure`; the controlled known-file and standard
response-size limits do not bound pure-headless CDP response materialization.
Subprocess stdout is streamed rather than accumulated by the server: generic
commands retain only a 64-KiB diagnostic tail, while Katana retains no duplicate
stdout because its sanitized JSONL is already written incrementally.

## Ordered Implementation Steps

### Step 1 — Freeze the E2E contract

Status on 2026-07-23: implemented. The authoritative run completed 24/24
targets through the crawler server, verified all 24 persisted jobs after a
server restart, found no cross-job isolation violation, and recorded 527
sitemap entries. The browser fixture oracle independently proves that the
currently missing runtime-XHR, rendered-navigation, cookie/`localStorage`,
header, multi-seed, and bounded-perpetual-traffic controls are reachable. See
`phase0-baseline-report.md` and `phase0-baseline-results.json`.

- Complete manifests for every target with health checks, resets,
  expected baseline and browser-only endpoints, declared action sequences,
  destructive-route markers, ledgers, and budgets. The current inventory has
  24 targets: 21 repository-controlled release fixtures and 3 observation-only
  public canaries. Gates use the manifest count rather than a hard-coded total.
- Ensure the running fixture set has deterministic markers for endpoints found
  only by `-jc`, only by `-jsl`, an HTML form emitted by `-fx`, a runtime-only
  XHR/fetch request, and a rendered-DOM-only navigation. Each marker must state
  which standard, pure-headless, or CDP evidence lane is expected to find it.
- Add controlled cases for multiple seeds, cookie and `localStorage` handoff,
  header-only authentication, perpetual browser traffic, and nested,
  redirected, malformed, oversized, and cyclic known files.
- Keep CrawlGround in the authoritative inventory and score its declared
  markers by discovery lane as well as by final sitemap coverage.
- Update the historical Phase 0 figures in this design and keep the current
  real-server report reproducible.
- Make target versions and container digests explicit.

Delivery: all repository-controlled sites and public canaries, including
CrawlGround, are running, health-checked, independently resettable where the
target supports it, and covered by the real-server E2E runner. The current
baseline and every lane-specific marker are reproducible before implementation
begins.

### Step 2 — Remove Proxify

Status on 2026-07-23: implemented and qualified through the real server. The
24-target direct-Katana run completed every job, verified every persisted
result after restart, found no cross-job isolation violation, and produced 304
sitemap entries. It retained 5/10 lane markers and CrawlGround 3/59, but found
0/10 browser-only controls. Persistent expected-route gaps were recorded for
site C, site F, four auth-flow intermediates, Juice Shop, and Parabank. Reruns
also showed that site D, security-question auth, and TOTP auth can vary from a
severely incomplete first result to full expected coverage, so those first-run
shortfalls are recorded as nondeterminism rather than confirmed lifecycle
defects. See `phase0-baseline-report.md` and
`phase0-baseline-results.json`.

- Stop starting, health-checking, and stopping the Proxify subprocess.
- Remove proxy flags, proxy environment variables, fixed-port assumptions,
  settings, Docker installation, documentation, and tests.
- Give each job an explicit Katana JSONL artifact path and build the sitemap from
  Katana output alone.
- Remove all legacy Proxify-log parsing and compatibility behavior.
- Run the complete real-server procedure on every manifest target before making
  the shared-browser change.

Delivery: a direct-Katana, no-Proxify E2E report for all targets. This is a
mechanical removal checkpoint, not the final `-cwu` baseline. The 223-entry
drop from the Step 1 proxy-observed run is not accepted as equivalent coverage:
Step 3 must determine which observations the direct Katana artifact omits, and
Steps 4 and 6 must recover relevant browser traffic through the shared Chrome
and passive CDP lane.

### Step 3 — Run a disposable Chrome, CDP, and Katana capability spike

Status on 2026-07-23: implemented and qualified against the exact static Katana
binary copied from the production crawler image. The spike passed 24/24 checks:
Playwright → Katana `-cwu` → Playwright retained cookies and origin
`localStorage`; passive CDP observed pages, popups, frames, workers, and service
workers; standard and pure-headless lanes exercised their assigned flags; both
lanes emitted `queue_exhausted` for every input; and pure-headless `-H` reached
the protected page with HTTP 200. See `browser-capability-spike-report.md` and
`browser-capability-spike-results.json`.

The build is derived from upstream source commit
`0265c675d03de83b1a1f1935ffb9c8ca9e4c17aa`. The vendored patch SHA-256 is
`a7b2c1cefed70d82360c2cc3088b810177e7e09b2f741e3727cb84cba93bca4c`;
the qualified Go 1.25.7 binary SHA-256 is
`49ab204962b91b4de9ee81b0f227716bae6f13ce71acadff60fe17e3ac1cb196`;
and the DIT model SHA-256 is
`eba238b3093ff7aa4772ce17536bc313cb955428a6aa87dae41695a2dede6e59`.

The current derived version is `v1.6.1-tenzai.2`. It prevents `-jsl` from
passing CSS responses to the JavaScript tree-sitter parser. App-010 reproduced
unbounded parser growth on a 687,309-byte stylesheet; with the fix, the exact
full standard lane exhausted its queue in 15.3 seconds at a 629,988-KiB peak
RSS under a 3-GiB diagnostic cap. CSS relative-endpoint extraction remains in
the regex parser enabled by `-jc`.

The spike also established four other implementation constraints. Nested sitemap
documents fetched by native `-kf` did not enqueue their declared URL marker;
pure-headless does not consume `-kf`; pure-headless accepts `-mrs` without
bounding CDP response materialization; and a real HTTP 200 can remain only a
request-only Katana JSONL record when process-wide uniqueness saw the URL
earlier. CDP response evidence is therefore retained, and explicit browser
seeds are ordered before a broad root seed. These are tested facts, not reasons
for the orchestrator to add its own URL deduplicator.

This step uses throwaway spike code and fixture invocations. It settles external
tool behavior and produces the pinned Katana artifact required by production,
but it does not pre-implement the orchestrator lifecycle owned by Step 4.

- Demonstrate cookies and origin `localStorage` surviving Playwright → Katana
  `-cwu` → a newly opened Playwright page. Explicitly prove that no later phase
  depends on old tabs, `sessionStorage`, open DOM state, or partial wizards.
- Prove the exclusive handoff: close Playwright pages, let Katana own all page
  targets, disconnect Katana without closing Chrome, then reconnect Playwright
  and open new pages.
- Prove passive CDP observation covers pages, popups, frames, workers, and
  service workers without blocking their requests, and emits XHR/fetch evidence
  with method and URL.
- Prove the standard pass honors `-jc`, `-jsl`, `-fx`, `-H`, `-td`, and
  `-mrs`; prove the pure-headless pass honors the flags assigned to it and runs
  multiple seeds serially with `-p 1`.
- Verify from `v1.6.1` source and runtime tests that `-kf` is absent from the
  pure-headless path and record the cases that the Step 6 controlled fetcher
  must cover.
- Implement and qualify the source-pinned pure-headless header correction.
- Add machine-readable terminal reasons to the source-pinned Katana build so
  queue exhaustion, input failure, crawl timeout, and cancellation cannot all
  look like exit 0.
- Qualify `-kb`, `-fpt`, `-fsu -fst 10`, `-iqp`, page-load strategy, clean
  exhaustion, deadlines, and every unwired flag listed in the capability
  matrix.

Delivery: a capability report; the pinned Katana base version, patch commit,
and binary checksum; the machine-readable terminal-summary schema; and both
final Katana commands. The report distinguishes standard, pure-headless,
passive-CDP, and accepted-but-ineffective capabilities. Spike code is not used
as the production browser lifecycle.

### Step 4 — Build the production shared browser lifecycle

Status on 2026-07-23: implemented and qualified through the real server. The
orchestrator now starts a job-private Chrome/profile and passive CDP observer,
passes its existing context into authentication, closes every Playwright page
before Katana `-cwu`, removes leftover page targets afterward, reconnects
Playwright, and navigates a fresh page before final cleanup. A 2-minute fixture
job completed with 18 entries and survived server-restart retrieval. A second
job reached `child.localhost` with the configured non-cookie auth header and
received HTTP 200. Both jobs left no Chrome, Katana process, or profile behind.
See `shared-browser-lifecycle-report.md`.

- Add orchestrator-owned Chrome startup, CDP endpoint discovery, and cleanup.
- Add passive CDP observation and crawl-scope authentication-header propagation.
- Refactor authentication to use the existing context without closing it.
- Enforce sequential Playwright/Katana ownership by closing Playwright pages
  before Katana and opening new pages after Katana.

Delivery: authenticated real-server jobs retain cookies and origin
`localStorage` across every boundary, including in-scope subdomains. New
Playwright pages work after every Katana pass; no tab reservation is required.

### Step 5 — Add API, checkpoint, and recovery behavior

Status on 2026-07-24: implemented, with the cancellation publication contract
corrected after review. All state transitions and checkpoint publication now go
through one persistence component, and the obsolete pre-discovery completion
path has been removed. The API persists and returns
the validated discovery configuration, rejects client limits above server caps,
and no longer accepts client-controlled Chrome/CDP options. The jobs schema
stores the immutable baseline, the latest validated partial discovery sitemap
and progress, evidence, and the final discovery result. Checkpoints are written
after browser additions and each enrichment lane. Restart, cancellation,
failure, the 60-minute whole-job deadline, and the shared 2-GiB process RSS
ceiling all finalize the newest valid checkpoint; a corrupt partial falls back
to the baseline, and a pre-checkpoint interruption fails. Legacy schemas remain
intentionally unsupported. See `discovery-checkpoint-recovery-report.md`.

- Add and validate the discovery request configuration.
- Add storage for the configuration, baseline checkpoint, raw evidence
  provenance, and discovery result metadata.
- Add `discovering`, partial finalization, and checkpoint-aware interrupted-job
  recovery.
- Keep completed sitemap `entries` and `tree` compatible.
- Return a valid persisted checkpoint sitemap for a cancelled job while keeping
  its public status `cancelled`.

Delivery: schema and real-server failure, restart, cancellation, and timeout
scenarios prove the documented checkpoint behavior before baseline code relies
on it.

### Step 6 — Build the dual-pass Katana baseline

Status on 2026-07-23: implemented and qualified. Controlled known-file
discovery enforces one global 100-document budget, a 5-MiB decoded-response
limit, nested indexes, redirects, diagnostics, and incremental in-scope origin
discovery. Site G proves that a subdomain first emitted by Katana receives its
own authenticated `robots.txt` and sitemap checks and that the resulting seed
reaches the next Katana lane. Standard and pure-headless passes use their
qualified flag sets. Per-seed `queue_exhausted` proves a complete lane; valid
`crawl_timeout` output is checkpointed and reported as partial rather than
failing the job.
The full proxyless baseline completed all 21 repository fixtures; CrawlGround
and ParaBank ended as observed public-canary failures after their strict
ten-minute pure-headless queue deadlines. Every one of the 24 job records was
retrieved after restart with zero isolation violations.

Review correction on 2026-07-24: baseline and enrichment use the same typed
Katana lane runner for browser epoch selection, crawl execution, artifact and
evidence parsing, and incremental known-file expansion. `run_job` delegates the
complete known-file plus dual-lane checkpoint lifecycle to
`run_baseline_phase()`.

- Add controlled known-file discovery with 100-document and 5-MiB limits.
- Prove this production fetcher closes the pure-headless `-kf` cases recorded by
  the Step 3 spike.
- Run standard Katana with JavaScript parsing, form extraction, classification,
  and technology detection. Add its valid discoveries to the baseline and
  candidate index, then run pure-headless Katana against the shared Chrome with
  the closed browser-seed batch.
- Capture runtime XHR/fetch evidence through passive CDP while pure-headless
  Katana runs.
- Trust each Katana pass for classification, uniqueness, and similar-URL
  filtering; do not claim path climbing or strategy flags in pure-headless.
- Normalize controlled-fetch, CDP, and Katana JSONL evidence without an
  orchestrator crawl-time URL-deduplication layer. Use only exact `(method,
  URL)` aggregation when publishing the sitemap.
- Persist the atomic baseline checkpoint.
- Run every manifest target and record the new authoritative proxyless
  baseline.

Delivery: a new full-inventory standard-plus-`-cwu` baseline with persisted
restart retrieval, observation accounting where ledgers exist, and zero
cross-job contamination. The report records the manifest total used by the run.

### Step 7 — Implement deterministic discovery

Status on 2026-07-23: implemented and qualified. Candidate ranking, exact
URL/state processing, deterministic actions, CDP attribution, stable seed
collection, two-lane enrichment, partial checkpoints, and fixpoint detection
run in the production orchestrator. The complete-system run found all 10/10
declared browser-only endpoints and all 5/5 required request sequences on
sites B/E/F without a destructive ledger hit.

Review correction on 2026-07-24: enrichment seeds now exclude candidate
navigation, subresources, and already-known GET endpoints; workflow counts are
computed from verified runtime effects rather than an adapter-owned counter.

- Consume Katana's candidate pages and knowledge-base classification without
  deduplicating or template-collapsing them.
- Track only exact LLM-processed URL/state pairs to avoid repeated model work.
- Add UI-state fingerprints and CDP evidence attribution.
- Implement sweep rounds, stable seed collection, Katana enrichment, and
  fixpoint detection.
- Exercise the production tool interface through a scripted model adapter.

Delivery: all 10 declared browser-only endpoints and all required request
sequences on sites B/E/F. Their ledgers report whether any destructive marker
was reached; the runtime does not block it.

### Step 8 — Integrate the live LLM

Status on 2026-07-23: implemented and qualified. The live discovery model uses
current element references, bounded page state, known endpoints, verified
action history, and remaining budgets. Malformed output, schema echo, stale
references, ineffective actions, and budget exhaustion have deterministic
coverage. Three live repetitions of each B/E/F fixture passed 9/9 runs, finding
30/30 browser-only endpoint instances and 15/15 request-sequence instances;
all nine results survived restart retrieval.

Review correction on 2026-07-24: the payload now includes bounded URL, title,
and visible-text context for each frame, keyed by the same frame prefix used by
element refs. The live adapter and its response schema now live in
`discovery_model.py`; browser capture and execution remain model-independent.
Select controls with no value different from the current selection are omitted
from model-eligible actions, preventing an impossible no-change retry loop.

- Add the discovery objective and prompt using the shared page-state tools.
- Enforce model turn, action, state, and time budgets outside the model.
- Cover malformed calls, prompt injection, stale references, ineffective loops,
  and budget exhaustion.
- Run three repeated live-model tests on the authoritative fixtures.

Delivery: repeatable live-model discovery using passive observation and Katana
as the crawl authority.

### Step 9 — Qualify the complete system

Status on 2026-07-23: implemented and qualified for the controlled release
boundary. The authoritative real-server run and targeted repaired-fixture
reruns produce `complete-system-qualification.md` and
`complete-system-qualification.json`: 21/21 controlled fixtures complete,
10/10 browser-only endpoints, 5/5 request sequences, 11/11 lane markers, 24/24
persisted results, and zero isolation violations. Juice Shop completes with a
bounded discovery outcome. CrawlGround and ParaBank remain visible failed
public canaries because pure-headless reports an incomplete queue after ten
minutes; they do not invalidate the controlled gate. CrawlGround preserves its
dual-pass baseline score of 44/59 but does not improve it.

- Run every manifest target sequentially through the real server.
- Restart the server and retrieve every persisted result.
- Exercise cancellation, timeout, partial failure, cleanup, and sequential-job
  isolation.
- Compare CrawlGround with the dual-pass baseline without hiding marker loss or
  incomplete termination. Improvement is a canary objective, not a controlled
  release gate.
- Publish machine-readable and Markdown qualification reports.

Delivery: final release evidence for functionality, observed destructive-route
behavior, persistence, and cleanup.

## E2E Test Authority

The authoritative test procedure is always:

1. start and health-check the targets;
2. start `uv run tenzai-crawler-server` with temporary DB and log paths;
3. wait for server readiness;
4. reset the target;
5. submit through `POST /jobs` or the CLI;
6. poll the real API until terminal;
7. compare the returned sitemap, discovery metadata, captured evidence, and
   target ledger;
8. restart the server and verify persisted retrieval;
9. stop everything and verify Chrome, Katana, profiles, and CDP observation
   sessions are gone.

No in-process FastAPI harness is an acceptance path.

### Required E2E gates

- All repository-controlled targets complete safely. Every public canary
  reaches a persisted terminal outcome, but a canary crawl failure remains
  non-blocking. The report records both boundaries from the loaded inventory
  rather than hard-coded counts.
- Sites B/E/F reach 10/10 declared browser-only endpoints and all terminal
  request sequences.
- Any fixture with a required interaction sequence reports at least one
  runtime-verified workflow; model- or adapter-declared counts are not accepted.
- Destructive-route ledger hits are reported explicitly; the runtime does not
  block them or hide them from results.
- Every controlled-target request is accounted for.
- Proxyless baseline entries remain a subset of final entries.
- Lane-specific fixtures prove standard Katana owns `-jc/-jsl/-fx`,
  pure-headless owns rendered navigation, and passive CDP owns runtime
  XHR/fetch evidence.
- Every Katana seed has a trustworthy terminal record, and no timed-out or
  failed pass is classified as a fixpoint.
- A new Playwright page can use the authenticated cookies and origin
  `localStorage` after every pure-headless pass; no test expects old tabs or
  `sessionStorage` to survive.
- Sequential jobs have no state or evidence contamination.
- Cancellation, timeout, partial failure, and restart follow the documented
  lifecycle.
- Deterministic scripted E2E passes reliably.
- Three live-model repetitions pass release qualification.
- CrawlGround coverage is compared with the new proxyless baseline without
  hiding a lost marker or an incomplete pass.
- Public canaries remain non-blocking and use observation-only runs.

### Existing-test policy

Existing tests are classified by contract, not age.

Keep or adapt:

- API validation and serialization;
- queue and cancellation behavior;
- DB and persisted sitemap behavior;
- authentication and secret non-persistence;
- subprocess cancellation and cleanup;
- manifest validation;
- sitemap schema and additive merge invariants.

Keep direct Playwright fixture tests only as fixture oracles. They prove that
the declared workflows are reachable but do not prove crawler behavior.

Rewrite or remove:

- delete Proxify tests and workarounds;
- rewrite crawler command tests around the standard and pure-headless `-cwu`
  commands;
- rewrite parser tests for controlled-fetch, Katana JSONL, and passive CDP
  evidence;
- rewrite orchestrator tests around the shared browser and checkpoint lifecycle;
- replace the in-process scenario test with real-server E2E;
- retain raw-Katana exclusion tests only as capability diagnostics.

The unit, lint, and type suites remain required hygiene. Obsolete architectural
expectations are rewritten or removed instead of constraining the new design.

## Verification

```bash
uv run pytest -q
uv run ruff check app tests scripts
uv run ty check
```

E2E additionally requires `RUN_E2E=1`, Katana in `PATH`, all controlled
targets running, and the crawler server using temporary DB and log paths.
Proxify is never required.
