extends Label

const SONG_FILE := "/tmp/current_song.txt"
const POLL_INTERVAL := 2.0
const FLASH_DURATION := 3.5
const FLASH_INTERVAL_START := 0.08
const FLASH_INTERVAL_END := 0.5

var poll_timer: float = 0.0
var fps_timer: float = 0.0
var current_song: String = ""
var flash_timer: float = 0.0
var flash_elapsed: float = 0.0
var flashing: bool = false
var flash_colors: Array[Color] = []
var flash_index: int = 0
var active_tween: Tween = null
var note_icon: TextureRect

const ICON_SIZE := 28
const ICON_GAP := 6

func _ready():
	add_theme_font_size_override("font_size", 24)
	add_theme_color_override("font_color", REST_COLOR)
	autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	max_lines_visible = 2

	var label_x := 20 + ICON_SIZE + ICON_GAP
	position = Vector2(label_x, 970)
	size = Vector2(270 - ICON_SIZE - ICON_GAP, 80)

	# Add music note icon as sibling, vertically aligned with first line
	note_icon = TextureRect.new()
	var img = Image.new()
	img.load("res://assets/music_note.svg")
	img.resize(ICON_SIZE, ICON_SIZE)
	var tex = ImageTexture.create_from_image(img)
	note_icon.texture = tex
	note_icon.position = Vector2(20, 970 + 2)
	note_icon.z_index = 20
	note_icon.modulate = REST_COLOR
	get_parent().call_deferred("add_child", note_icon)

func _process(delta):
	poll_timer -= delta
	if poll_timer <= 0:
		poll_timer = POLL_INTERVAL
		_read_song()

	fps_timer -= delta
	if fps_timer <= 0:
		fps_timer = 60.0
		print("[godot] FPS: %.1f" % Engine.get_frames_per_second())

	if flashing:
		flash_elapsed += delta
		flash_timer -= delta
		if flash_elapsed >= FLASH_DURATION:
			flashing = false
			_start_fade()
		elif flash_timer <= 0:
			# Ease-out: fast flashes early, slowing as we approach the fade.
			var t: float = clamp(flash_elapsed / FLASH_DURATION, 0.0, 1.0)
			var eased: float = 1.0 - pow(1.0 - t, 2.0)
			flash_timer = lerp(FLASH_INTERVAL_START, FLASH_INTERVAL_END, eased)
			flash_index = (flash_index + 1) % flash_colors.size()
			add_theme_color_override("font_color", flash_colors[flash_index])
			if note_icon:
				note_icon.modulate = flash_colors[flash_index]

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
	text = title
	modulate.a = 1.0
	if note_icon:
		note_icon.modulate = Color(1, 1, 1, 1)

	if active_tween and active_tween.is_valid():
		active_tween.kill()

	# Flash through round colors
	flash_colors = _get_round_colors()
	flash_index = 0
	flash_timer = 0.0
	flash_elapsed = 0.0
	flashing = true
	add_theme_color_override("font_color", flash_colors[0])

const REST_COLOR := Color(1.0, 1.0, 1.0, 1.0)
const COLOR_CROSSFADE := 1.2

func _set_label_color(c: Color):
	add_theme_color_override("font_color", c)
	if note_icon:
		note_icon.modulate = c

func _start_fade():
	modulate.a = 1.0
	var from_color: Color = flash_colors[flash_index]
	active_tween = create_tween()
	active_tween.tween_method(_set_label_color, from_color, REST_COLOR, COLOR_CROSSFADE)
