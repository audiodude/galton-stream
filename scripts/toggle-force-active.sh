#!/bin/bash
set -e

usage() {
    echo "Usage: $0 <on|off>"
    echo "  on  - set FORCE_ACTIVE=1 on both services (stream immediately, ignore window)"
    echo "  off - delete FORCE_ACTIVE on both services (resume normal window schedule)"
    exit 1
}

[ $# -eq 1 ] || usage
case "$1" in
    on|off) ACTION=$1 ;;
    *) usage ;;
esac

PROJECT_ID="31d9edbd-a224-4890-b9f5-fca2af5d649b"
ENV_ID="af2cba58-2abb-45ec-86ff-c5416af574fd"
STREAM_SVC="4fa8d6ab-8b64-4e99-b161-70370174b0d6"
MONITOR_SVC="7cf592be-134d-47e9-90eb-e9f73b2dcdf6"
API="https://backboard.railway.com/graphql/v2"
TOKEN=$(jq -r '.user.accessToken // .user.token' ~/.railway/config.json 2>/dev/null)

if [ -z "$TOKEN" ]; then
    echo "Error: no Railway token in ~/.railway/config.json. Run 'railway login' first."
    exit 1
fi

gql() {
    curl -s -X POST "$API" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d "$1"
}

upsert_var() {
    local svc_id=$1 name=$2 value=$3
    local payload
    payload=$(jq -n --arg p "$PROJECT_ID" --arg e "$ENV_ID" --arg s "$svc_id" --arg n "$name" --arg v "$value" \
        '{query: "mutation($input: VariableUpsertInput!) { variableUpsert(input: $input) }", variables: {input: {projectId: $p, environmentId: $e, serviceId: $s, name: $n, value: $v}}}')
    local resp
    resp=$(gql "$payload")
    if ! echo "$resp" | jq -e '.data.variableUpsert' > /dev/null; then
        echo "  upsert failed: $resp"
        return 1
    fi
}

delete_var() {
    local svc_id=$1 name=$2
    local payload
    payload=$(jq -n --arg p "$PROJECT_ID" --arg e "$ENV_ID" --arg s "$svc_id" --arg n "$name" \
        '{query: "mutation($input: VariableDeleteInput!) { variableDelete(input: $input) }", variables: {input: {projectId: $p, environmentId: $e, serviceId: $s, name: $n}}}')
    local resp
    resp=$(gql "$payload")
    if ! echo "$resp" | jq -e '.data.variableDelete' > /dev/null; then
        echo "  delete failed: $resp"
        return 1
    fi
}

redeploy() {
    local svc_id=$1
    local payload
    payload=$(jq -n --arg s "$svc_id" --arg e "$ENV_ID" \
        '{query: "mutation { serviceInstanceRedeploy(serviceId: \"" + $s + "\", environmentId: \"" + $e + "\") }"}')
    gql "$payload" > /dev/null
}

if [ "$ACTION" = "on" ]; then
    echo "Setting FORCE_ACTIVE=1 on galton-stream..."
    upsert_var "$STREAM_SVC" "FORCE_ACTIVE" "1"
    echo "Setting FORCE_ACTIVE=1 on galton-monitor..."
    upsert_var "$MONITOR_SVC" "FORCE_ACTIVE" "1"
    echo "Redeploying galton-stream..."
    redeploy "$STREAM_SVC"
    echo "Redeploying galton-monitor..."
    redeploy "$MONITOR_SVC"
    echo "Done. Stream will be live in ~30s, ignoring the operational window."
else
    echo "Deleting FORCE_ACTIVE on galton-stream..."
    delete_var "$STREAM_SVC" "FORCE_ACTIVE"
    echo "Deleting FORCE_ACTIVE on galton-monitor..."
    delete_var "$MONITOR_SVC" "FORCE_ACTIVE"
    echo "Redeploying galton-stream..."
    redeploy "$STREAM_SVC"
    echo "Redeploying galton-monitor..."
    redeploy "$MONITOR_SVC"
    echo "Done. Normal window schedule resumed (11:45-18:05 PT)."
fi
