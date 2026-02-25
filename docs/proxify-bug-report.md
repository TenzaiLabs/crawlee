# POST request bodies are dropped during forwarding (Transfer-Encoding: chunked rewrite sends empty body)

**Project:** [projectdiscovery/proxify](https://github.com/projectdiscovery/proxify)

## Environment

- **Proxify version:** v0.0.16 (latest release)
- **OS:** Ubuntu 24.04 (amd64)
- **Go version:** 1.21+

## Description

Proxify silently drops POST request bodies when forwarding to upstream servers. During forwarding, it rewrites the `Content-Length` header to `Transfer-Encoding: chunked` but only sends the empty chunk terminator (`0\r\n\r\n`), discarding the actual body content. The upstream receives a well-formed but empty HTTP request.

> **Note:** [PR #656](https://github.com/projectdiscovery/proxify/pull/656) fixed body *logging* in JSONL output, but the *forwarding* bug persists. This is the other half of the problem originally reported in [issue #558](https://github.com/projectdiscovery/proxify/issues/558).

## Steps to Reproduce

### 1. Start proxify

```bash
proxify -http-addr 127.0.0.1:8888 -output /tmp/proxify-test.jsonl
```

### 2. Start a raw TCP listener to inspect what proxify forwards

```python
import socket

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 9999))
s.listen(1)
conn, _ = s.accept()
print(conn.recv(4096).decode())
conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
conn.close()
s.close()
```

### 3. Send a POST request through the proxy

```bash
curl -x http://127.0.0.1:8888 -X POST \
  -d "username=admin&password=secret" \
  http://127.0.0.1:9999/login
```

### 4. Observe the listener output

```
POST /login HTTP/1.1
Host: 127.0.0.1:9999
User-Agent: curl/8.5.0
Transfer-Encoding: chunked
Accept: */*
Content-Type: application/x-www-form-urlencoded

0

```

`Content-Length` was replaced with `Transfer-Encoding: chunked` and only the terminating `0` chunk was sent. The body `username=admin&password=secret` is gone.

## Expected Behavior

The upstream server should receive the original POST body intact — either preserved with the original `Content-Length` header, or properly chunked with the body content followed by the chunk terminator.

## Actual Behavior

The body is silently dropped. The upstream receives a syntactically valid but empty HTTP request, causing it to process as if no form data was submitted. No error is returned to the client.

## Impact

This breaks **any use case involving POST requests** through the proxy:

- **Form-based authentication** — login forms submit empty credentials
- **API calls** — POST/PUT payloads are lost
- **Webhook forwarding** — event data is discarded

The failure is silent: the client receives a normal response from the upstream, which simply acted on an empty body.

## Workaround

Bypass the proxy for POST/PUT requests and route them directly to the upstream.

## Related Issues

- [#558](https://github.com/projectdiscovery/proxify/issues/558) — Request bodies missing from logs (the logging half was fixed; the forwarding half was not)
- [PR #656](https://github.com/projectdiscovery/proxify/pull/656) — Fixed body capture in JSONL output
