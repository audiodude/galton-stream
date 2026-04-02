# Galton Board Simulator

A Godot 4.x app that simulates a Galton board (bean machine). Balls drop through pegs and collect in bins, forming a bell curve. Designed to run 24/7 and be streamed to YouTube via OBS.

## Features

- Balls spawn continuously from a funnel at the top
- 12 rows of pegs with concave curved walls
- Two randomly-selected colors per round with smooth transitions
- Bins show blended colors from all balls that landed in them
- Histogram overlay scales to full bin height
- Pegs flash subtly when touched
- Auto-resets after 500 balls, waiting for all balls to clear the pegs
- Ball/cycle counter

## Requirements

- Godot 4.4+

## Running

```bash
godot --path . --main-scene main.tscn
```

Press ESC to quit.

## Streaming

Use OBS to capture the Godot window and stream to YouTube. Recommended settings:

- Encoder: x264, CBR 4500 Kbps
- Resolution: 1920x1080
- FPS: 30
- Tune: zerolatency
