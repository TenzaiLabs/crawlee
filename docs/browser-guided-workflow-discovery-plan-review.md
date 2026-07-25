# Browser-Guided Workflow Discovery Plan Review

## Verdict

**Change requested before implementation.** The document's explicit Katana
matrix is mostly accurate, but the design still has five high-severity blockers:

1. stock v1.6.1 pure-headless cannot apply `-H` headers;
2. Katana destroys Playwright-owned tabs and cannot preserve `sessionStorage` or
   open wizard state;
3. default JSONL persists credentials, cookies, request bodies, and raw traffic;
4. the multi-seed command omits required `-p 1`;
5. `-ct 5m` does not bound a pass, and Katana cannot reliably report complete
   versus truncated traversal.

## Katana v1.6.1 source audit

| Claim/flag | Source-backed result |
| --- | --- |
| `-cwu` | **Confirmed.** A non-empty Chrome WebSocket URL selects pure-headless before hybrid/standard and attaches Rod directly to that browser ([engine dispatch](https://github.com/projectdiscovery/katana/blob/v1.6.1/internal/runner/runner.go#L93-L106), [browser attach](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/browser/browser.go#L112-L195)). |
| `-kf` | **Confirmed gap.** Known-file setup is in `common.Shared`; pure-headless does not construct it. Passing `-kf` still has the surprising side effect of raising depth to 3 ([validation](https://github.com/projectdiscovery/katana/blob/v1.6.1/internal/runner/options.go#L101-L104), [common crawl session](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/common/base.go#L318-L353)). |
| `-kb` | **Confirmed, but incomplete in the document.** It classifies unique intercepted responses and can emit both `PageType` and `Forms`; it does not affect traversal ([classification](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/types/crawler_options.go#L253-L282), [headless callback](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/headless.go#L161-L178)). |
| `-fpt` | **Confirmed.** Output filtering only, after the response was fetched, parsed, and classified ([writer filter](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/output/output.go#L200-L210)). |
| `-fsu/-fst` | **Partially described.** The trie affects emitted/parser-discovered URL uniqueness, not browser-action scheduling. Similar-looking URLs may still be clicked or loaded ([URL filter](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/headless.go#L233-L257), [action queue](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/crawler/crawler.go#L446-L468)). |
| `-iqp` | **Confirmed with the same qualification.** It strips query values only for output uniqueness; distinct query values may still be navigated. |
| `-td` | **Confirmed ignored.** Wappalyzer is initialized, but pure-headless never invokes it. |
| `-pc` | **Confirmed ignored.** Path climbing exists only in the common queue path ([common enqueue](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/common/base.go#L165-L200)). |
| `-mrs` | **Confirmed ignored.** Pure-headless retrieves entire response bodies through CDP without a limit ([response interception](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/browser/browser.go#L522-L609)). |
| `-s` | **Confirmed ignored.** Pure-headless always uses a linked FIFO action queue. It is breadth-first by action discovery, but state restoration/replay means network-request order is not simple URL BFS ([queue loop](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/crawler/crawler.go#L175-L264)). |
| `-d` | **Honored as action depth**, not HTTP-resource or parser-discovery depth ([depth check](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/crawler/crawler.go#L251-L267)). |
| `-rl/-hrl` | **Confirmed ignored.** Limiters are constructed, but pure-headless never consumes them. Chrome document and subresource traffic is unthrottled. |
| `-mdp` | **Confirmed ignored.** It means maximum pages per domain, not duration, and enforcement is only in the common path. |
| `-pls` | **Confirmed active.** `domcontentloaded` is misleadingly named: it calls Rod `WaitLoad` and then sleeps `-dwt`, rather than waiting specifically for the DOMContentLoaded event ([load strategies](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/browser/browser.go#L239-L321)). |
| `-jc/-jsl` | **Ignored due to a pure-headless wiring bug.** Additional analysis creates a fresh parser without the configured optional JS parsers ([headless parser creation](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/headless.go#L244-L257)). |
| `-fx/-xhr` | **Ignored.** Pure-headless does not receive either option. Actual XHR responses may still appear as ordinary intercepted records, but not under `xhr_requests`. |
| `-no-incognito` | **No effect with `-cwu`.** It only changes Katana's launch-new-Chrome branch. |
| `-H` | **Ignored.** Custom headers are not passed into pure-headless crawler/browser options. |
| `-mfc 10` | **Active.** Stops after ten consecutive action failures and returns success, even with queued actions remaining. |
| `-ct 5m` | **Active per seed URL**, not per CLI pass. With \(N\) serial seeds, nominal runtime can approach \(N \times 5\) minutes. |
| Traversal across seeds | Default `-p` is 10. Each seed creates a separate target/crawler. With `-p 1`, seeds run serially and share only process-wide emitted-URL filtering. |

# High findings — confirmed defects

## H1 — Header authentication is impossible with stock v1.6.1

**Document:** [CDP observation and authentication propagation](browser-guided-workflow-discovery-plan.md#cdp-observation-and-authentication-propagation), [Authentication handoff](browser-guided-workflow-discovery-plan.md#authentication-handoff)

The document says non-cookie headers are passed with `-H`. Pure-headless never
forwards `CustomHeaders` into its browser options
([headless option handoff](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/headless.go#L111-L192)).
This breaks bearer, Basic, API-key, CSRF, and the controlled fixture's
`X-Crawler-Test-Run` header.

A generic `Network.setExtraHTTPHeaders` workaround is not acceptable as written:
it would affect out-of-scope third-party resources because pure-headless scope is
checked after browser egress. It would also violate the deliberate passive-CDP
boundary.

**Implementation consequence:** manual-header and captured-header jobs silently
crawl unauthenticated; ledger-based E2E can produce empty ledgers.

**Required document change:**

- Remove the claim that `-H` works.
- Add `-H` to the capability matrix as ineffective.
- Explicitly choose one compatible mechanism before Step 4. The cleanest option
  consistent with the fixed decisions is a source-pinned v1.6.1-based Katana
  build that applies headers conditionally inside Katana using the same scope
  validator. Pin its commit and checksum.
- If upstream v1.6.1 must remain byte-identical, this requirement needs a
  separately qualified browser-native mechanism; stock Katana plus passive CDP
  cannot implement it.
- Add same-host, matching-subdomain, and out-of-scope-resource leakage tests.

This is one deliberate decision that is **technically impossible with unmodified
v1.6.1**.

## H2 — Katana cannot preserve Playwright tabs, `sessionStorage`, or open wizard state

**Document:** [Shared browser lifecycle](browser-guided-workflow-discovery-plan.md#shared-browser-lifecycle), [Step 4 delivery](browser-guided-workflow-discovery-plan.md#step-4--build-the-shared-browser-lifecycle)

Katana creates a fresh target in the default context
([target creation](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/browser/browser.go#L404-L445)).
On every pool return it closes **every page except its current page**, not merely
targets it owns
([cleanup loop](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/browser/browser.go#L644-L672)).

A new target also does not inherit another independent tab's `sessionStorage`;
that storage is partitioned by top-level browsing context and destroyed when its
tab closes ([MDN](https://developer.mozilla.org/en-US/docs/Web/API/Window/sessionStorage)).

**Implementation consequence:**

- authentication/discovery tabs are destroyed during Katana;
- in-progress DOM/wizard state and tab-scoped session state disappear;
- cookies and origin-scoped `localStorage` can survive, but `localStorage` is not
  propagated across subdomains.

**Required document change:**

- Define a **Katana-exclusive target epoch**: Playwright must quiesce and close
  all pages before each Katana pass; after Katana exits, Playwright creates fresh
  pages.
- Narrow Step 4's delivery to cookies, cache/profile state, and per-origin
  `localStorage`.
- Remove promises to preserve `sessionStorage`, open tabs, and wizard DOM state.
- If those are hard requirements, Katana must be patched to reuse an owned
  Playwright target and to stop closing foreign target IDs.

## H3 — Default JSONL violates the plaintext-secret guardrail

**Document:** [Katana command](browser-guided-workflow-discovery-plan.md#katana-baseline-and-enrichment), [Persistence and result contract](browser-guided-workflow-discovery-plan.md#persistence-and-result-contract), [Step 2 artifact](browser-guided-workflow-discovery-plan.md#step-2--remove-proxify)

Pure-headless constructs JSONL containing:

- `Cookie`, `Authorization`, and other request headers;
- `Set-Cookie` response headers;
- POST bodies;
- full response bodies;
- full raw request and response messages.

See
[response construction](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/browser/browser.go#L540-L578).
Raw/body omission is opt-in
([output mutation](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/output/output.go#L234-L242)).

Therefore "all other Katana evidence may be retained as emitted" is incompatible
with "plaintext credentials, cookie and authorization values … never persisted."

**Implementation consequence:** passwords, session cookies, bearer tokens, CSRF
tokens, and application-returned secrets can land in `/data/logs` before any
later parser redaction.

**Required document change:**

- Katana must not write unsanitized output directly to an artifact.
- Pipe stdout into an in-memory allow-list sanitizer before any file, log tail,
  error message, or database write.
- Use `-or -ob -eof body,raw,forms,xhr_requests` as defense in depth. The
  sanitizer may read content type and `knowledgebase.PageType`, then discard all
  headers and the rest of the knowledge base.
- Never enable `-sr`, diagnostics, `-kb-secrets`, debug logging, or unsanitized
  output tails.
- Define narrow URL-query secret removal because retained URLs can themselves
  carry credentials.
- Call this evidence minimization required by the existing secret exception, not
  general evidence redaction.

Stock JSONL cannot simultaneously retain method, URL, status, content type, and
page type while proving all secret-bearing fields were omitted; a streaming
sanitizer or Katana patch is required.

## H4 — The multi-seed command must use `-p 1`

**Document:** [Controlled known-file discovery](browser-guided-workflow-discovery-plan.md#controlled-known-file-discovery), [Katana command](browser-guided-workflow-discovery-plan.md#katana-baseline-and-enrichment), [Discovery rounds](browser-guided-workflow-discovery-plan.md#candidate-selection-and-discovery-rounds)

Katana's default input parallelism is 10
([CLI defaults](https://github.com/projectdiscovery/katana/blob/v1.6.1/cmd/katana/main.go#L218-L227)).
Every concurrent seed creates its own target in the same Chrome, and each seed's
pool cleanup closes the other seeds' pages.

**Implementation consequence:** known-file and enrichment seeds terminate one
another, produce target-closed errors, and yield nondeterministic incomplete
results.

**Required document change:**

```text
-p 1
```

must be in every `-cwu` command. Clarify that:

- inputs are batch-read before crawling begins;
- each seed has an independent action queue;
- Katana's process-wide output filter remains authoritative across those serial
  seeds;
- `-p 1` does not solve Playwright-tab destruction, hence the exclusive-target
  handoff in H2 remains necessary.

## H5 — The five-minute pass and fixpoint semantics are false

**Document:** [Discovery rounds](browser-guided-workflow-discovery-plan.md#candidate-selection-and-discovery-rounds), [Budgets](browser-guided-workflow-discovery-plan.md#budgets), [Persistence outcomes](browser-guided-workflow-discovery-plan.md#persistence-and-result-contract)

`-ct 5m` creates a timeout inside each seed's `Crawler.Crawl`, not around the
complete multi-seed process
([per-seed timeout](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/crawler/crawler.go#L209-L239)).
Both `-ct` expiry and `-mfc` return `nil`. The runner also logs and discards
per-seed crawl errors before exiting successfully
([runner behavior](https://github.com/projectdiscovery/katana/blob/v1.6.1/internal/runner/executer.go#L37-L59)).

JSONL has no terminal record distinguishing:

- queue exhaustion;
- duration truncation;
- maximum failures;
- CDP loss;
- response-body read failure;
- output failure.

**Implementation consequence:** a 100-document sitemap can turn a nominal
five-minute pass into hours, and a truncated crawl can be mislabeled `fixpoint`.

**Required document change:**

- Define a hard orchestrator-owned, monotonic **whole-process** deadline separate
  from the existing inactivity timeout in
  [`run_safe_subprocess`](../app/process.py#L102-L121).
- Use `-mfc 0`, or treat any `-mfc` event as an incomplete pass.
- Do not claim `-ct` bounds a pass.
- Only permit `fixpoint` after a trustworthy queue-exhaustion signal. Prefer
  adding a machine-readable per-seed terminal summary in the same source-pinned
  Katana build required by H1.
- Deadline, CDP disconnect, sanitizer failure, missing terminal summaries, or
  forced termination must produce `partial_failure`/`interrupted`, never
  `fixpoint`.
- After SIGTERM/SIGKILL, explicitly enumerate and close leftover Katana targets
  before Playwright resumes.

# Medium findings — confirmed defects or missing decisions

## M1 — The command advertises five ineffective capabilities

**Document:** [Katana command and capability matrix](browser-guided-workflow-discovery-plan.md#katana-baseline-and-enrichment), [Candidate ranking](browser-guided-workflow-discovery-plan.md#candidate-selection-and-discovery-rounds)

`-no-incognito`, `-jc`, `-jsl`, `-fx`, and `-xhr` are ineffective under `-cwu`,
contradicting the claim that the production command contains only source-verified
capabilities.

**Consequence:** candidate ranking cannot rely on Katana's configured form, XHR,
or static-JS extraction.

**Document change:**

- Remove those flags.
- Add them to the matrix.
- State that forms come from `-kb` and live Playwright page state; XHR/runtime
  endpoints come from passive CDP evidence and ordinary intercepted records.
- Static JS-source extraction is unavailable in this engine unless Katana is
  patched.

## M2 — Path climbing is claimed despite being unavailable

**Document:** [Summary](browser-guided-workflow-discovery-plan.md#summary), [Step 5](browser-guided-workflow-discovery-plan.md#step-5--build-the-pure-headless-katana-baseline)

Step 5 promises Katana-owned path climbing, but `-pc` is ignored and absent from
the command. The summary also says "similar-page filtering" when the implemented
feature is similar-**URL** output filtering.

**Consequence:** path ancestors will not be added, and `-fsu` must not be treated
as reducing browser actions or request volume.

**Document change:** remove path climbing from Step 5, change "similar-page" to
"similar-URL," and state that `-fsu` affects emitted URL uniqueness only. Do not
move path climbing into the orchestrator; that would violate the fixed ownership
decision.

## M3 — Scope and redirect language overstates enforcement

**Document:** [Katana trust boundary](browser-guided-workflow-discovery-plan.md#katana-trust-boundary), [Summary](browser-guided-workflow-discovery-plan.md#summary)

In pure-headless:

- Chrome follows redirects; `-dr` is ignored;
- intermediate redirect responses are skipped;
- scope is checked after egress for responses and after actions for resulting
  pages;
- forms, buttons, scripts, subresources, and redirects can contact out-of-scope
  hosts.

See
[redirect continuation](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/browser/browser.go#L522-L527)
and
[post-response scope filtering](https://github.com/projectdiscovery/katana/blob/v1.6.1/pkg/engine/headless/headless.go#L129-L146).

Katana also uses response-stage `Fetch` interception and hardcoded cookie-consent
request blocking. Thus the orchestrator observer can be passive, but **CDP use
overall is not passive**.

**Document change:** explicitly say:

- "passive" applies only to the orchestrator observer;
- Katana's internal CDP implementation pauses responses and may block
  cookie-consent requests;
- scope is an action-admission/output boundary, not an egress firewall;
- redirects are browser-followed and intermediate responses require CDP
  evidence.

If "CDP is passive" is intended to include Katana itself, v1.6.1 pure-headless is
technically incompatible with that decision.

## M4 — Newly discovered subdomain seeds cannot enter the active pass

**Document:** [Controlled known-file discovery](browser-guided-workflow-discovery-plan.md#controlled-known-file-discovery), [one-pass baseline](browser-guided-workflow-discovery-plan.md#katana-baseline-and-enrichment)

Katana reads stdin completely before starting and has no live seed-admission API
([input parsing](https://github.com/projectdiscovery/katana/blob/v1.6.1/internal/runner/options.go#L130-L155)).
A subdomain learned during a pass cannot have its sitemap URLs added to that same
pass.

**Consequence:** "every URL is passed to Katana" conflicts with "one
pure-headless pass."

**Document change:** choose one sequence explicitly:

1. prefetch known files for already-known origins;
2. close the seed batch;
3. run Katana;
4. fetch known files for newly emitted subdomain origins;
5. pass those URLs to the **next** enrichment pass.

If all such URLs must belong to baseline, permit additional baseline passes and
remove the one-pass claim.

Also define whether the 5 MiB limit applies to compressed or decoded bytes, how
redirect/index cycles are counted, and whether failed attempts consume the
100-document budget.

## M5 — Additive merge is underspecified under "no orchestrator deduplication"

**Document:** [Candidate selection](browser-guided-workflow-discovery-plan.md#candidate-selection-and-discovery-rounds), [Persistence](browser-guided-workflow-discovery-plan.md#persistence-and-result-contract), [Step 5](browser-guided-workflow-discovery-plan.md#step-5--build-the-pure-headless-katana-baseline)

Katana's unique filter resets between processes/rounds. The same page can
therefore be emitted in baseline and enrichment. A final additive merge,
`new_entry_count`, and baseline-subset assertion require an identity rule.

**Consequence:** either duplicate public entries are produced or the
implementation quietly recreates a URL-dedup policy.

**Document change:** distinguish:

- immutable Katana page/evidence records, retaining pass provenance and never
  template-collapsed; from
- exact public sitemap endpoint aggregation required to maintain stable
  `entries`/`tree`.

Explicitly permit exact endpoint identity merge while prohibiting a second
similar-URL/page uniqueness policy.

## M6 — Chrome loss, CDP loss, and forced cleanup lack recovery policy

**Document:** [Shared browser lifecycle](browser-guided-workflow-discovery-plan.md#shared-browser-lifecycle), [Persistence recovery](browser-guided-workflow-discovery-plan.md#persistence-and-result-contract)

Katana has no reconnect logic. CDP loss often becomes ordinary action failures
and can still yield exit 0. SIGKILL leaves Chrome and Katana targets alive;
SIGTERM cleanup is not guaranteed because Katana calls `os.Exit(0)` while
per-seed goroutines may still be active.

**Document change:**

- Browser/observer disconnect cancels the pass immediately.
- Do not relaunch Chrome and claim session recovery after browser loss.
- Before checkpoint: fail the job.
- After checkpoint: finalize as `partial_failure` or `interrupted`.
- Retry initial Katana connection only if no navigation/request has occurred.
- After forced termination, close all remaining page targets, then reconnect
  Playwright.
- Final cleanup must terminate Katana's process group, Chrome, CDP listeners, and
  delete the profile even when an earlier phase failed.

## M7 — The authoritative E2E inventory is missing required coverage

**Document:** [Step 1](browser-guided-workflow-discovery-plan.md#step-1--freeze-the-e2e-contract), [E2E authority](browser-guided-workflow-discovery-plan.md#e2e-test-authority)

The current target schema has no ledger endpoint, action sequence, destructive
marker, or per-target budget fields
([manifest loader](../scripts/run_phase0_baseline.py#L90-L143)). Only B/E/F have
request ledgers, and those ledgers ignore requests without
`X-Crawler-Test-Run`, for example
[Site B](../testsites/site-b-login-flask/app.py#L31-L51). The present real-server
baseline does not send that header, read ledgers, or assert process/profile
cleanup.

There is no controlled coverage for:

- parent-domain/subdomain auth propagation and third-party leakage;
- `localStorage` versus `sessionStorage`;
- service workers and dedicated/shared workers;
- cross-origin/OOPIF frames;
- large, streaming, SSE, download, cached, and service-worker responses;
- same-URL GET/POST/XHR uniqueness;
- CDP death, `-mfc`, body-read loss, output failure, or forced Katana kill;
- nested, redirected, malformed, compressed, or cyclic known files.

**Document change:** add those targets or scenarios before calling the 23-target
run authoritative. Use a ledger correlation mechanism that works with the final
header solution; until then, a fixture cookie or per-target exclusive ledger is
safer than assuming `-H`.

The E2E procedure must also restart the server **during** authenticating,
crawling, discovering, and processing—not only after terminal jobs—and inspect
Chrome/Katana PIDs, the CDP port, and profile paths after every failure mode.

## M8 — The persistence step is ordered after a step that already requires it

**Document:** [Step 5](browser-guided-workflow-discovery-plan.md#step-5--build-the-pure-headless-katana-baseline), [Step 6](browser-guided-workflow-discovery-plan.md#step-6--add-api-and-recovery-behavior)

Step 5 persists and restart-retrieves a baseline checkpoint. Step 6 then adds the
checkpoint schema and recovery behavior. These are not independently
deliverable.

The current schema has only one final `sitemap` column
([database schema](../app/db.py#L35-L69)), and startup currently marks every
active job `failed_interrupted` unconditionally
([startup recovery](../app/main.py#L203-L214)).

**Recommended nine-step order:**

1. Freeze and extend the real-server E2E contract.
2. Remove Proxify; state explicitly which temporary existing Katana engine is
   retained for this mechanical checkpoint.
3. Run a disposable capability spike and settle the exact Katana
   build/header/terminal-status contract.
4. Build shared Chrome lifecycle, exclusive target handoff, and passive observer.
5. Add API, status, checkpoint schema, and checkpoint-aware startup recovery.
6. Build the pure-headless baseline and write the checkpoint.
7. Implement deterministic discovery.
8. Integrate the live LLM.
9. Qualify the complete system.

Step 3 must be labeled a spike harness; otherwise it depends on the production
lifecycle nominally built in Step 4.

## M9 — Pure-headless still has an unbounded per-response memory risk

**Document:** [Known-file limits and Katana matrix](browser-guided-workflow-discovery-plan.md#katana-baseline-and-enrichment), [Budgets](browser-guided-workflow-discovery-plan.md#budgets)

The 5 MiB limit protects only controlled known-file fetches. `-mrs` does not
protect Katana: CDP bodies are fully materialized before output omission or KB
classification.

**Consequence:** one large/streaming response can OOM Katana despite every
documented budget.

**Document change:** add a job-level process/container memory ceiling and
classify an OOM/kill as partial failure. This is not a page-count or request-count
limit and does not reverse that deliberate decision.

# Low findings

## L1 — Depth and load-strategy wording should be exact

**Document:** [Katana command](browser-guided-workflow-discovery-plan.md#katana-baseline-and-enrichment), [fallback](browser-guided-workflow-discovery-plan.md#katana-baseline-and-enrichment)

Specify that:

- `-d` limits Katana action depth, not browser subresources or parser-discovered
  endpoints;
- `-pls domcontentloaded` actually waits for `load` and then sleeps;
- pure-headless traversal is FIFO by actions regardless of `-s`;
- state restoration/replay can cause additional navigations outside simple FIFO
  request order.

## L2 — Browser endpoint discovery and KB assets need pinning details

**Document:** [Shared browser lifecycle](browser-guided-workflow-discovery-plan.md#shared-browser-lifecycle), [Step 1](browser-guided-workflow-discovery-plan.md#step-1--freeze-the-e2e-contract)

Parsing the DevTools URL only from Chromium stderr is less robust than reading
the job-private profile's `DevToolsActivePort` file with a startup timeout. Also
specify whether `-kb` needs downloaded/cached classifier data and how that
artifact is pinned for offline, repeatable E2E.

# Experiments that must be run

These validate workarounds or undocumented runtime interactions; they do not
supersede the confirmed source defects.

1. **Scoped header mechanism — release blocker:** prove document, XHR, iframe,
   worker, and matching-subdomain requests receive auth headers while
   out-of-scope third-party requests do not.
2. **Exclusive target handoff:** confirm cookies and origin `localStorage`
   survive Playwright → Katana → Playwright, while no design depends on tab
   `sessionStorage` or open DOM state.
3. **Passive observer coverage:** use browser-level target auto-attach without
   pausing requests and measure whether initial navigation, popups, OOPIFs,
   workers, service workers, redirects, and cached responses are all observed.
   Strict 100% accounting may require a Katana-side event feed because passive
   post-creation attachment has a race.
4. **Multiple CDP clients:** verify the observer's `Network` sessions do not
   interfere with Katana's response-stage `Fetch` interception.
5. **Termination classification:** exercise queue exhaustion, external deadline,
   `-mfc`, CDP loss, body-read failure, output-sink failure, SIGTERM, and SIGKILL;
   prove only genuine exhaustion can produce `fixpoint`.
6. **Output semantics:** test same-URL GET/POST/XHR, redirects, 204/304,
   downloads, large binaries, streams, SSE, WebSockets, and service-worker
   responses. Determine which record wins first-seen uniqueness.
7. **Known files:** test robots-only, sitemap index recursion, redirects, cycles,
   gzip, malformed XML, oversized decoded content, and subdomains learned only
   after Katana starts.
8. **Load strategy:** qualify `heuristic` and the fallback against perpetual
   polling, WebSockets, SSE, delayed DOM mutation, and client-side route changes.
9. **Cleanup:** with `-p 1`, force termination at every lifecycle boundary and
   verify no Katana target, Chrome process, CDP listener, profile directory, or
   secret-bearing artifact remains.
