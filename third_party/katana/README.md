# Tenzai Katana build

The crawler image builds Katana from the upstream `v1.6.1` source commit
`0265c675d03de83b1a1f1935ffb9c8ca9e4c17aa`, then applies
`tenzai-v1.6.1.patch` (SHA-256
`a7b2c1cefed70d82360c2cc3088b810177e7e09b2f741e3727cb84cba93bca4c`).

The patch:

- propagates `-H` headers into pure-headless pages used through `-cwu`;
- emits atomic, per-input terminal reasons through `-terminal-summary`;
- distinguishes queue exhaustion, crawl timeout, input failure, and process
  cancellation;
- prevents `-jsl` from feeding CSS responses into its JavaScript tree-sitter
  parser while leaving CSS relative-endpoint extraction to `-jc`;
- identifies the derived binary as `v1.6.1-tenzai.2`.

Katana's `-kb` and `-fpt` flags require the DIT classifier model, which the
upstream release archive does not contain. The image downloads the model from
the DIT project's published model URL and verifies SHA-256
`eba238b3093ff7aa4772ce17536bc313cb955428a6aa87dae41695a2dede6e59` before
installing it as `/root/.dit/model.json`.

The source patch and model are qualified by
`scripts/run_browser_capability_spike.py`; its current evidence is in
`docs/browser-capability-spike-report.md` and
`docs/browser-capability-spike-results.json`. The exact static binary produced
from the pinned source with Go 1.25.7 has SHA-256
`49ab204962b91b4de9ee81b0f227716bae6f13ce71acadff60fe17e3ac1cb196`.

The `tenzai.2` parser regression was reproduced against App-010's 687,309-byte
stylesheet. The unpatched `-jsl` path exhausted memory inside `gotreesitter`;
the patched full standard lane exhausted its crawl queue in 15.3 seconds with
a 629,988-KiB peak RSS under a 3-GiB diagnostic cap. The older capability-spike
reports remain historical evidence for the `tenzai.1` build.
