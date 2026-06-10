#!/usr/bin/env bash
# render_piece.sh <model> <preset> <seed> <duration_sec> <out_dir> [--kind piece|ident]
# Renders a single vxstory model+preset to 720p60 mp4 with seed substitution.
set -euo pipefail

MODEL="${1:?usage: render_piece.sh <model> <preset> <seed> <duration_sec> <out_dir> [--kind piece|ident]}"
PRESET="${2:?}"
SEED="${3:?}"
DURATION="${4:?}"
OUT_DIR="${5:?}"
KIND="piece"

shift 5
while [[ $# -gt 0 ]]; do
    case "$1" in
        --kind) KIND="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

VXSTORY_DIR="${VXSTORY_DIR:-$(cd "$(dirname "$0")/../../.." && pwd)/vxstory}"
MODEL_DIR="$VXSTORY_DIR/$MODEL"
PRESET_PATH="$MODEL_DIR/presets/$PRESET.json"

[[ -d "$MODEL_DIR" ]] || { echo "model dir not found: $MODEL_DIR" >&2; exit 1; }
[[ -f "$PRESET_PATH" ]] || { echo "preset not found: $PRESET_PATH" >&2; exit 1; }

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"  # resolve to absolute

OUT_NAME="${MODEL}_${PRESET}_s${SEED}"
OUT_MP4="$OUT_DIR/${OUT_NAME}.mp4"
OUT_JSON="$OUT_DIR/${OUT_NAME}.json"

# Skip if already rendered
if [[ -f "$OUT_MP4" ]]; then
    echo "[render_piece] skipping (exists): $OUT_MP4"
    exit 0
fi

# Temp files
TMP_PRESET="$(mktemp /tmp/preset_XXXXXX.json)"
TMP_AVI="$(mktemp /tmp/render_XXXXXX.avi)"
OVERRIDE_CFG="$MODEL_DIR/override.cfg"

cleanup() {
    rm -f "$TMP_PRESET" "$TMP_AVI" "$OVERRIDE_CFG"
}
trap cleanup EXIT

# Substitute seed into preset
jq --argjson seed "$SEED" '.seed = $seed' "$PRESET_PATH" > "$TMP_PRESET"

# Write override.cfg for 720p capture
cat > "$OVERRIDE_CFG" <<'EOF'
[display]
window/size/viewport_width=1280
window/size/viewport_height=720
EOF

echo "[render_piece] rendering $MODEL/$PRESET seed=$SEED duration=${DURATION}s -> $OUT_MP4"
START_TIME="$(date +%s)"

xvfb-run -a -s "-screen 0 1280x720x24" \
    godot --path "$MODEL_DIR" \
    --write-movie "$TMP_AVI" \
    --fixed-fps 60 \
    -- --preset "$TMP_PRESET" --duration "$DURATION"

ELAPSED=$(( $(date +%s) - START_TIME ))
echo "[render_piece] godot done in ${ELAPSED}s, encoding mp4..."

ffmpeg -y -loglevel error \
    -i "$TMP_AVI" \
    -c:v libx264 -crf 18 -pix_fmt yuv420p \
    -r 60 -an \
    "$OUT_MP4"

# Write sidecar JSON (duration via ffprobe)
ACTUAL_DUR="$(ffprobe -v error -select_streams v:0 \
    -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$OUT_MP4")"

RENDERED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
jq -n \
    --arg id "${OUT_NAME}" \
    --arg kind "$KIND" \
    --arg model "$MODEL" \
    --arg preset "$PRESET" \
    --argjson seed "$SEED" \
    --argjson duration_sec "$ACTUAL_DUR" \
    --arg file "$OUT_MP4" \
    --arg rendered_at "$RENDERED_AT" \
    '{id: $id, kind: $kind, model: $model, preset: $preset, seed: $seed,
      duration_sec: $duration_sec, file: $file, rendered_at: $rendered_at}' \
    > "$OUT_JSON"

TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
echo "[render_piece] done in ${TOTAL_ELAPSED}s: $OUT_MP4"
