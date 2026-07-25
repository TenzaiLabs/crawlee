<!--
Katana currently uses a discussion-first workflow and disables direct issue
creation. Post this at:
https://github.com/projectdiscovery/katana/discussions/new?category=q-a

Suggested title:
`-jsl` parses CSS with the JavaScript tree-sitter parser and exhausts memory

Before posting, attach the local reproduction fixture `styles.css` referenced
below. Maintainers can convert a confirmed bug discussion into an issue.
-->

### katana version:

`v1.6.1` (`0265c675d03de83b1a1f1935ffb9c8ca9e4c17aa`), the latest release as of
2026-07-25.

The reproduction used ProjectDiscovery's official Linux amd64 release:

- Release archive SHA-256: `503754f1bd370c3ef287df6998e317baed2dd75bdd13ea64034f09b80ca393f3`
- Extracted `katana` SHA-256: `0827ac620fcf8c507062465c14b16435377e6d53d2c4dcebac3fbee1115bd61e`
- Environment: Ubuntu 24.04, Linux x86_64 under WSL2

### Current Behavior:

When `-jsl` is enabled, Katana sends responses whose path ends in `.css` to
JSLuice's JavaScript tree-sitter parser. A real 687,309-byte minified
stylesheet causes rapid memory growth and an out-of-memory failure.

Under a 3-GiB virtual-memory diagnostic limit, the official `v1.6.1` binary
exits with status 2 after 4.67 seconds. GNU `time` reports a peak RSS of
1,535,512 KiB. The Go runtime reports 1,944,780,800 bytes in use when its next
4-MiB allocation fails.

The failure stack identifies the complete path and exact input size:

```text
runtime: out of memory: cannot allocate 4194304-byte block (1944780800 in use)
fatal error: out of memory

github.com/odvcencio/gotreesitter.(*Parser).parseInternal(
    ..., {0xc001cc8000, 0xa7ccd, 0xa8000}, ...)
github.com/odvcencio/gotreesitter.(*Parser).Parse(...)
github.com/Mzack9999/jsluice.NewAnalyzer(...)
github.com/projectdiscovery/katana/pkg/utils.ExtractJsluiceEndpoints(...)
github.com/projectdiscovery/katana/pkg/engine/parser.scriptJSFileJsluiceParser(...)
```

`0xa7ccd` is 687,309 bytes, exactly matching the stylesheet size.

The current eligibility check explicitly accepts both `.js` and `.css`:

https://github.com/projectdiscovery/katana/blob/0265c675d03de83b1a1f1935ffb9c8ca9e4c17aa/pkg/engine/parser/parser_generic.go#L56-L69

The same check remains present on the `dev` branch:

https://github.com/projectdiscovery/katana/blob/dev/pkg/engine/parser/parser_generic.go#L56-L69

### Expected Behavior:

The JSLuice JavaScript parser should process only responses whose URL path has
a JavaScript extension or whose content type indicates JavaScript. A `.css`
response with a CSS content type should not be parsed as JavaScript.

CSS relative-endpoint extraction can remain handled by the regex parser enabled
by `-jc`; excluding CSS from `-jsl` does not require excluding it from Katana's
CSS endpoint discovery.

### Steps To Reproduce:

1. Download and extract the official release:

   ```bash
   curl -fLO https://github.com/projectdiscovery/katana/releases/download/v1.6.1/katana_1.6.1_linux_amd64.zip
   unzip katana_1.6.1_linux_amd64.zip -d katana-v1.6.1
   ./katana-v1.6.1/katana -version
   ```

2. Place the attached `styles.css` in an empty directory. Verify the fixture:

   ```bash
   wc -c styles.css
   sha256sum styles.css
   ```

   Expected output:

   ```text
   687309 styles.css
   4784e1c8574403418dcba86dee7b4e59d5f604a3d95a236b8d6f97028881b518  styles.css
   ```

3. Serve the fixture locally:

   ```bash
   python3 -m http.server 8000 --bind 127.0.0.1
   ```

4. In another terminal, run the official binary with `-jsl` under the
   diagnostic memory limit:

   ```bash
   (
     ulimit -v 3145728
     /usr/bin/time -v ./katana-v1.6.1/katana \
       -u http://127.0.0.1:8000/styles.css \
       -jsl \
       -silent
   )
   ```

5. Observe the out-of-memory failure in `gotreesitter` and
   `scriptJSFileJsluiceParser`.

6. Repeat without `-jsl` as a control:

   ```bash
   (
     ulimit -v 3145728
     /usr/bin/time -v ./katana-v1.6.1/katana \
       -u http://127.0.0.1:8000/styles.css \
       -silent
   )
   ```

   The control completes normally with exit status 0 and a 59,648-KiB peak
   RSS.

### Anything else:

The isolated measurements were:

| Binary and flags | Exit | Elapsed | Peak RSS |
| --- | ---: | ---: | ---: |
| Official `v1.6.1`, `-jsl` | 2 | 4.67 s | 1,535,512 KiB |
| Official `v1.6.1`, without `-jsl` | 0 | 10.34 s | 59,648 KiB |
| Locally patched `v1.6.1`, `-jsl` | 0 | 10.44 s | 59,564 KiB |

The locally tested fix removes `.css` from the JSLuice eligibility condition
while preserving JavaScript content-type detection:

```diff
- if !stringsutil.HasSuffixAny(resp.Resp.Request.URL.Path, ".js", ".css") &&
-    !strings.Contains(contentType, "/javascript") {
+ path := strings.ToLower(resp.Resp.Request.URL.Path)
+ contentType := strings.ToLower(resp.Resp.Header.Get("Content-Type"))
+ if !strings.HasSuffix(path, ".js") &&
+    !strings.Contains(contentType, "/javascript") {
      return
  }
```

A focused regression test should verify that:

- `.css` with `text/css` is rejected by `scriptJSFileJsluiceParser`;
- `.js` is accepted even when the server uses a generic content type;
- JavaScript content types remain accepted for extensionless or misnamed URLs;
- CSS remains available to the `-jc` regex parser.

I searched the titles and bodies of all 113 public Katana Discussions, plus the
public issue tracker, for reports involving JSLuice, CSS, `gotreesitter`,
tree-sitter, and out-of-memory failures. I did not find an existing report for
CSS responses being passed to `scriptJSFileJsluiceParser`.

The closest reports have different causes:

- Discussion #1710 reports hybrid-crawler memory growth caused by an infinite
  stream of randomly generated links, rather than CSS parsing:
  https://github.com/projectdiscovery/katana/discussions/1710
- Discussion #1469 reports inconsistent `-jsluice` output, without CSS or an
  out-of-memory failure:
  https://github.com/projectdiscovery/katana/discussions/1469
- Discussion #901 concerns building the `go-tree-sitter` dependency and was
  answered by enabling CGO:
  https://github.com/projectdiscovery/katana/discussions/901
- Issue #658 reports an OOM while crawling a list of 18,000 URLs, without this
  parser path:
  https://github.com/projectdiscovery/katana/issues/658
