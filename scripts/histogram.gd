extends Node2D

var bin_counts: Array[int] = []
var bin_colors: Array[Color] = []
var max_count: int = 1
var board_offset_x: float = 240.0
var board_width: float = 1440.0
var bin_top_y: float = 0.0
var fade: float = 1.0:
	set(v):
		fade = v
		queue_redraw()

func update_data(counts: Array[int], colors: Array[Color], offset_x: float, width: float, top_y: float = 0.0):
	bin_counts = counts
	bin_colors = colors
	board_offset_x = offset_x
	board_width = width
	bin_top_y = top_y
	max_count = 1
	for c in counts:
		if c > max_count:
			max_count = c
	queue_redraw()

func _draw():
	if bin_counts.is_empty():
		return
	var bin_width := board_width / bin_counts.size()
	var base_y := 1080.0
	var bar_max_height := base_y - bin_top_y

	for i in range(bin_counts.size()):
		if bin_counts[i] == 0:
			continue
		var height = (float(bin_counts[i]) / max_count) * bar_max_height
		var rect = Rect2(
			board_offset_x + i * bin_width,
			base_y - height,
			bin_width,
			height
		)

		# Use the blended bin color
		var bar_color = bin_colors[i] if i < bin_colors.size() else Color.WHITE
		bar_color.a = 0.6 * fade
		draw_rect(rect, bar_color)

