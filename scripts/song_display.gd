extends Label

const SONG_FILE := "/tmp/current_song.txt"
const POLL_INTERVAL := 2.0
const FLASH_DURATION := 1.5
const FLASH_INTERVAL := 0.1

var poll_timer: float = 0.0
var current_song: String = ""
var flash_timer: float = 0.0
var flash_elapsed: float = 0.0
var flashing: bool = false
var flash_colors: Array[Color] = []
var flash_index: int = 0
var active_tween: Tween = null

func _ready():
	add_theme_font_size_override("font_size", 20)
	add_theme_color_override("font_color", Color(0.6, 0.65, 0.8, 0.7))
	position = Vector2(20, 1045)
	size = Vector2(600, 30)

func _process(delta):
	poll_timer -= delta
	if poll_timer <= 0:
		poll_timer = POLL_INTERVAL
		_read_song()

	if flashing:
		flash_elapsed += delta
		flash_timer -= delta
		if flash_elapsed >= FLASH_DURATION:
			flashing = false
			_start_fade()
		elif flash_timer <= 0:
			flash_timer = FLASH_INTERVAL
			flash_index = (flash_index + 1) % flash_colors.size()
			add_theme_color_override("font_color", flash_colors[flash_index])

func _read_song():
	if not FileAccess.file_exists(SONG_FILE):
		return
	var file = FileAccess.open(SONG_FILE, FileAccess.READ)
	if file == null:
		return
	var title = file.get_as_text().strip_edges()
	file.close()

	if title != current_song and not title.is_empty():
		current_song = title
		_show_song(title)

func _get_round_colors() -> Array[Color]:
	var main_node = get_parent()
	if main_node and "current_colors" in main_node:
		return main_node.current_colors
	return [Color.WHITE] as Array[Color]

func _show_song(title: String):
	text = "♪ " + title
	modulate.a = 1.0

	if active_tween and active_tween.is_valid():
		active_tween.kill()

	# Flash through round colors
	flash_colors = _get_round_colors()
	flash_index = 0
	flash_timer = 0.0
	flash_elapsed = 0.0
	flashing = true
	add_theme_color_override("font_color", flash_colors[0])

func _start_fade():
	add_theme_color_override("font_color", Color(0.6, 0.65, 0.8, 0.7))
	modulate.a = 1.0
	# Fade to 0.4 opacity over 10s
	active_tween = create_tween()
	active_tween.tween_property(self, "modulate:a", 0.4, 10.0).set_ease(Tween.EASE_IN).set_trans(Tween.TRANS_QUAD)
