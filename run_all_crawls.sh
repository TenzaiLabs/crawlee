#!/usr/bin/env bash
set -euo pipefail

API="http://localhost:8000"
SITEMAP_DIR="$(cd "$(dirname "$0")/testsites" && pwd)"

PASS=0
FAIL=0
TOTAL=0
RESULTS=""

# validate_sitemap <site_dir_name> <status_response_json>
# Extracts expected URLs from testsites/<dir>/sitemap.json, compares against
# the crawl result's sitemap entries. Reports missing expected URLs.
validate_sitemap() {
  local site_dir="$1"
  local response_json="$2"
  local sitemap_path="${SITEMAP_DIR}/${site_dir}/sitemap.json"

  if [ ! -f "$sitemap_path" ]; then
    echo "    (no sitemap.json to validate against)"
    return 0
  fi

  python3 -c "
import json, sys

with open('${sitemap_path}') as f:
    expected = json.load(f)
response = json.loads(sys.argv[1])

expected_urls = set()
for e in expected.get('entries', []):
    url = e.get('url', '')
    if url:
        expected_urls.add(url.rstrip('/'))
blocked_urls = set()
for e in expected.get('blocked_entries', []):
    url = e.get('url', '')
    if url:
        blocked_urls.add(url.rstrip('/'))

actual_sitemap = response.get('sitemap') or {}
actual_entries = actual_sitemap.get('entries') or []
actual_urls = set()
for e in actual_entries:
    url = e.get('url', '') if isinstance(e, dict) else str(e)
    if url:
        actual_urls.add(url.rstrip('/'))

found = expected_urls & actual_urls
missing = expected_urls - actual_urls
extra = actual_urls - expected_urls
blocked = blocked_urls & actual_urls

print(f'    Expected: {len(expected_urls)} URLs')
print(f'    Found:    {len(found)}/{len(expected_urls)} expected URLs')
if blocked_urls:
    print(f'    Blocked:  {len(blocked)}/{len(blocked_urls)} forbidden URLs crawled')
if missing:
    print(f'    Missing:  {len(missing)} URLs')
    for u in sorted(missing):
        print(f'      - {u}')
if blocked:
    print(f'    Forbidden URLs crawled: {len(blocked)}')
    for u in sorted(blocked):
        print(f'      - {u}')
if extra:
    print(f'    Extra:    {len(extra)} URLs (not in sitemap.json)')

if missing or blocked:
    sys.exit(1)
" "$response_json"
}

crawl() {
  local name="$1"
  local site_dir="$2"
  local payload="$3"

  TOTAL=$((TOTAL + 1))
  echo ""
  echo "=========================================="
  echo "[$TOTAL] CRAWLING: $name"
  echo "=========================================="

  # Submit the job
  local response
  response=$(curl -s -X POST "$API/jobs" \
    -H "Content-Type: application/json" \
    -d "$payload")

  local job_id
  job_id=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])" 2>/dev/null)
  if [ -z "$job_id" ]; then
    echo "  ERROR: Failed to create job. Response: $response"
    FAIL=$((FAIL + 1))
    RESULTS="${RESULTS}\n  FAIL  $name - could not create job"
    return
  fi

  echo "  Job ID: $job_id"

  # Poll until terminal state (max 600 seconds)
  local status="pending"
  local status_response=""
  for i in $(seq 1 600); do
    sleep 1
    status_response=$(curl -s "$API/jobs/$job_id")
    status=$(echo "$status_response" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null)
    if [[ "$status" == "completed" || "$status" == "failed" || "$status" == "failed_interrupted" || "$status" == "cancelled" ]]; then
      break
    fi
  done

  if [ "$status" != "completed" ]; then
    local error
    error=$(echo "$status_response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error',''))" 2>/dev/null || echo "")
    echo "  FAILED (status=$status) error=$error"
    FAIL=$((FAIL + 1))
    RESULTS="${RESULTS}\n  FAIL  $name - status=$status error=$error"
    return
  fi

  local entries
  entries=$(echo "$status_response" | python3 -c "
import sys, json
data = json.load(sys.stdin)
sm = data.get('sitemap') or {}
entries = sm.get('entries') or []
print(len(entries))
" 2>/dev/null || echo "?")
  echo "  Completed - ${entries} entries crawled"

  # Validate against expected sitemap
  if validate_sitemap "$site_dir" "$status_response"; then
    echo "  RESULT: PASS"
    PASS=$((PASS + 1))
    RESULTS="${RESULTS}\n  PASS  $name - ${entries} entries"
  else
    echo "  RESULT: FAIL (sitemap contract violation)"
    FAIL=$((FAIL + 1))
    RESULTS="${RESULTS}\n  FAIL  $name - ${entries} entries (sitemap contract violation)"
  fi
}

# ── Site crawls ──────────────────────────────────────────────────────

crawl "site-a-static" "site-a-static" \
  '{"target_url": "http://localhost:8001"}'

crawl "site-b-login-flask" "site-b-login-flask" \
  '{"target_url": "http://localhost:8002",
    "auth_config": {
      "login_url": "http://localhost:8002/login",
      "credentials": {"username": "demo", "password": "password"}
    }}'

crawl "site-c-registration-express" "site-c-registration-express" \
  '{"target_url": "http://localhost:8003"}'

crawl "site-d-complex-auth-go" "site-d-complex-auth-go" \
  '{"target_url": "http://localhost:8004",
    "auth_config": {
      "login_url": "http://localhost:8004/login",
      "credentials": {"username": "admin", "password": "swordfish"},
      "instructions": "There are two submit buttons. Click the Sign In button, not Register."
    }}'

crawl "site-e-crawl-trap-ruby" "site-e-crawl-trap-ruby" \
  '{"target_url": "http://localhost:8005"}'

crawl "site-f-spa-deno" "site-f-spa-deno" \
  '{"target_url": "http://localhost:8006"}'

# ── Auth crawls ──────────────────────────────────────────────────────

crawl "auth-a-simple-form" "auth-a-simple-form" \
  '{"target_url": "http://localhost:8101",
    "auth_config": {
      "login_url": "http://localhost:8101/login",
      "credentials": {"email": "auth-a-simple-form@auth.local", "password": "pa$$w0rd"}
    }}'

crawl "auth-b-http-basic" "auth-b-http-basic" \
  '{"target_url": "http://localhost:8102",
    "auth_config": {
      "headers": ["Authorization: Basic dXNlcjpwYXNz"]
    }}'

crawl "auth-c-complex-form" "auth-c-complex-form" \
  '{"target_url": "http://localhost:8103",
    "auth_config": {
      "login_url": "http://localhost:8103/login",
      "credentials": {"email": "auth-c-complex-form@auth.local", "password": "pa$$w0rd"},
      "instructions": "Fill in the email and password fields. You may also need to fill tenant and region fields if present — use any valid value."
    }}'

crawl "auth-d-interactive-captcha" "auth-d-interactive-captcha" \
  '{"target_url": "http://localhost:8104",
    "auth_config": {
      "login_url": "http://localhost:8104/login",
      "credentials": {"email": "auth-d-interactive-captcha@auth.local", "password": "pa$$w0rd", "challenge_code": "588357"},
      "instructions": "Enter the email and password. There is also an interactive challenge code field — enter 588357."
    }}'

crawl "auth-e-delay-login" "auth-e-delay-login" \
  '{"target_url": "http://localhost:8105",
    "auth_config": {
      "login_url": "http://localhost:8105/login",
      "credentials": {"email": "auth-e-delay-login@auth.local", "password": "pa$$w0rd"},
      "instructions": "The login form may appear after a delay. Wait for it to appear, then fill in email and password and submit."
    }}'

crawl "auth-f-ocr-captcha" "auth-f-ocr-captcha" \
  '{"target_url": "http://localhost:8106",
    "auth_config": {
      "login_url": "http://localhost:8106/login",
      "credentials": {"email": "auth-f-ocr-captcha@auth.local", "password": "pa$$w0rd", "captcha_code": "4319"},
      "instructions": "Enter email and password. There is a captcha code field — enter 4319."
    }}'

crawl "auth-g-multi-step" "auth-g-multi-step" \
  '{"target_url": "http://localhost:8107",
    "auth_config": {
      "login_url": "http://localhost:8107/login",
      "credentials": {"email": "auth-g-multi-step@auth.local", "password": "pa$$w0rd"},
      "instructions": "This is a multi-step login. First enter email and submit, then enter password on the next step and submit."
    }}'

crawl "auth-h-new-window" "auth-h-new-window" \
  '{"target_url": "http://localhost:8108",
    "auth_config": {
      "login_url": "http://localhost:8108/login",
      "credentials": {"email": "auth-h-new-window@auth.local", "password": "pa$$w0rd"},
      "instructions": "The login may open in a popup window. Fill in email and password and submit the form."
    }}'

crawl "auth-i-iframe" "auth-i-iframe" \
  '{"target_url": "http://localhost:8109",
    "auth_config": {
      "login_url": "http://localhost:8109/login",
      "credentials": {"email": "auth-i-iframe@auth.local", "password": "pa$$w0rd"},
      "instructions": "The login form is inside an iframe. Fill in email and password and submit."
    }}'

crawl "auth-j-xsrf-token" "auth-j-xsrf-token" \
  '{"target_url": "http://localhost:8110",
    "auth_config": {
      "login_url": "http://localhost:8110/login",
      "credentials": {"email": "auth-j-xsrf-token@auth.local", "password": "pa$$w0rd"},
      "instructions": "Fill in email and password and submit. The form handles XSRF tokens automatically."
    }}'

crawl "auth-k-dynamic-fields" "auth-k-dynamic-fields" \
  '{"target_url": "http://localhost:8111",
    "auth_config": {
      "login_url": "http://localhost:8111/login",
      "credentials": {"email": "auth-k-dynamic-fields@auth.local", "password": "pa$$w0rd"},
      "instructions": "The form field names are dynamically generated. Look for input fields by their type or placeholder text rather than name. Fill in the email/username and password fields and submit."
    }}'

crawl "auth-l-security-question" "auth-l-security-question" \
  '{"target_url": "http://localhost:8112",
    "auth_config": {
      "login_url": "http://localhost:8112/login",
      "credentials": {"email": "auth-l-security-question@auth.local", "password": "pa$$w0rd", "security_answer": "42"},
      "instructions": "Fill in email, password, and the security question answer. The answer is: 42"
    }}'

crawl "auth-m-totp-mfa" "auth-m-totp-mfa" \
  '{"target_url": "http://localhost:8113",
    "auth_config": {
      "login_url": "http://localhost:8113/login",
      "credentials": {"email": "auth-m-totp-mfa@auth.local", "password": "pa$$w0rd", "totp_seed": "I65VU7K5ZQL7WB4E", "totp_code": "{{totp_seed:I65VU7K5ZQL7WB4E}}"},
      "instructions": "Fill in email and password and submit. If prompted for a TOTP/MFA code, use the generated code from the provided seed."
    }}'

crawl "auth-o-bearer-token" "auth-o-bearer-token" \
  '{"target_url": "http://localhost:8115",
    "auth_config": {
      "headers": ["Authorization: Bearer t0k3nId"]
    }}'

echo ""
echo "=========================================="
echo "SUMMARY: $PASS passed, $FAIL failed (out of $TOTAL)"
echo "=========================================="
echo -e "$RESULTS"
