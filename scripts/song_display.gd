extends Label

const SONG_FILE := "/tmp/current_song.txt"
const POLL_INTERVAL := 2.0

var poll_timer: float = 0.0
var current_song: String = ""

func _ready():
	add_theme_font_size_override("font_size", 20)
	add_theme_color_override("font_color", Color(0.6, 0.65, 0.8, 0.5))
	position = Vector2(20, 1045)
	size = Vector2(600, 30)

func _process(delta):
	poll_timer -= delta
	if poll_timer <= 0:
		poll_timer = POLL_INTERVAL
		_read_song()

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

func _show_song(title: String):
	text = "♪ " + title
	modulate.a = 1.0
	add_theme_color_override("font_color", Color.WHITE)
	# Full opacity for 5s, then ease-in to 0.8 over 10s, hold
	var tween = create_tween()
	tween.tween_interval(5.0)
	tween.tween_property(self, "modulate:a", 0.4, 10.0).set_ease(Tween.EASE_IN).set_trans(Tween.TRANS_QUAD)
