#!/usr/bin/env bash
# publish.sh <version> <changelog> <files...>
# uploads plugin files to orca cloud as a new version, proving who we are with
# github's own oidc token (orca cloud's "publish from github" trusted publishing)
set -euo pipefail
version="$1"; changelog="$2"; shift 2
oidc_token="$(curl -sS -H "Authorization: Bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=orcacloud" | jq -r .value)"
metadata="$(jq -cn --arg version "$version" --arg changelog "$changelog" \
  '{version: $version} + (if $changelog == "" then {} else {changelog: $changelog} end)')"
args=(--form-string "metadata=$metadata")
for f in "$@"; do args+=(-F "files=@$f"); done
[ "${#args[@]}" -gt 1 ] || { echo "no files to publish" >&2; exit 1; }
status="$(curl -sS -o response.json -w '%{http_code}' -H "Authorization: Bearer $oidc_token" \
  "${args[@]}" https://api.orcaslicer.com/api/v1/plugin-publish/releases)"
cat response.json; echo
[ "$status" = "201" ]
