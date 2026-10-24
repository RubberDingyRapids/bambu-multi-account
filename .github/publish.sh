#!/usr/bin/env bash
# publish.sh <version> <changelog> <files...>
# uploads plugin files to orca cloud as a new version, proving who we are with
# github's own oidc token (orca cloud's "publish from github" trusted publishing)
set -euo pipefail
version="$1"; changelog="$2"; shift 2
metadata="$(jq -cn --arg version "$version" --arg changelog "$changelog" \
  '{version: $version} + (if $changelog == "" then {} else {changelog: $changelog} end)')"
args=(--form-string "metadata=$metadata")
for f in "$@"; do args+=(-F "files=@$f"); done
[ "${#args[@]}" -gt 1 ] || { echo "no files to publish" >&2; exit 1; }

# orca cloud runs a security scan on every upload; when that scanner is down it answers
# SECURITY_SCAN_UNAVAILABLE ("try again shortly"), so wait and try again, up to ~40 min.
# a fresh oidc token each try, github's ones only last a few minutes.
for attempt in 1 2 3 4 5 6 7 8 9; do
  oidc_token="$(curl -sS -H "Authorization: Bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
    "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=orcacloud" | jq -r .value)"
  status="$(curl -sS -o response.json -w '%{http_code}' -H "Authorization: Bearer $oidc_token" \
    "${args[@]}" https://api.orcaslicer.com/api/v1/plugin-publish/releases || echo 000)"
  cat response.json 2>/dev/null || true; echo
  if [ "$status" = "201" ]; then exit 0; fi
  if grep -q SECURITY_SCAN_UNAVAILABLE response.json 2>/dev/null || [ "${status:0:1}" = "5" ] || [ "$status" = "000" ]; then
    echo "orca cloud not ready (http $status), try $attempt of 9, waiting 5 min"
    sleep 300
    continue
  fi
  exit 1
done
echo "gave up after 9 tries" >&2
exit 1
