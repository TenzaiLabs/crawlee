# Browser-Guided Discovery Qualification

Generated: `2026-07-24T10:53:04.201570+00:00`

Every row used a real `uv run tenzai-crawler-server`, HTTP job submission, target reset and ledger, persisted API retrieval after server restart, and the server-owned live discovery model.

- Passed: 9/9 runs.
- Browser-only endpoints: 30/30.
- Browser-only endpoints observed in guided-browser traffic: 30/30.
- Required guided request sequences: 15/15.
- Required ledger requests: 27.
- Destructive ledger requests observed: 0.
- Runtime-verified workflows: 23.
- Persisted results verified: 9/9.

| Target | Run | Status | Outcome | Entries | New | Browser-only | Guided | Sequences | States | Workflows | Baseline kept | Persisted | Destructive |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |
| `site-b-login-flask` | 1 | `completed` | `fixpoint` | 16 | 3 | 4/4 | 4/4 | 2/2 | 16 | 2 | yes | yes | 0 |
| `site-e-crawl-trap-ruby` | 1 | `completed` | `fixpoint` | 13 | 1 | 1/1 | 1/1 | 1/1 | 10 | 2 | yes | yes | 0 |
| `site-f-spa-deno` | 1 | `completed` | `fixpoint` | 18 | 5 | 5/5 | 5/5 | 2/2 | 14 | 4 | yes | yes | 0 |
| `site-b-login-flask` | 2 | `completed` | `fixpoint` | 16 | 3 | 4/4 | 4/4 | 2/2 | 16 | 2 | yes | yes | 0 |
| `site-e-crawl-trap-ruby` | 2 | `completed` | `fixpoint` | 13 | 1 | 1/1 | 1/1 | 1/1 | 10 | 2 | yes | yes | 0 |
| `site-f-spa-deno` | 2 | `completed` | `fixpoint` | 18 | 5 | 5/5 | 5/5 | 2/2 | 12 | 3 | yes | yes | 0 |
| `site-b-login-flask` | 3 | `completed` | `fixpoint` | 16 | 3 | 4/4 | 4/4 | 2/2 | 16 | 2 | yes | yes | 0 |
| `site-e-crawl-trap-ruby` | 3 | `completed` | `fixpoint` | 13 | 1 | 1/1 | 1/1 | 1/1 | 10 | 2 | yes | yes | 0 |
| `site-f-spa-deno` | 3 | `completed` | `fixpoint` | 18 | 5 | 5/5 | 5/5 | 2/2 | 13 | 4 | yes | yes | 0 |

## Gaps

No qualification gaps.

Machine-readable results: `docs/browser-guided-discovery-qualification.json`
