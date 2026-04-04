extends Node

signal chat_event(event: Dictionary)

const EVENTS_FILE := "/tmp/chat_events.json"
const POLL_INTERVAL := 1.0

var poll_timer: float = 0.0

func _process(delta):
	poll_timer -= delta
	if poll_timer <= 0:
		poll_timer = POLL_INTERVAL
		_read_events()

func _read_events():
	if not FileAccess.file_exists(EVENTS_FILE):
		return
	var file = FileAccess.open(EVENTS_FILE, FileAccess.READ)
	if file == null:
		return
	var text = file.get_as_text()
	file.close()

	# Delete file so we don't re-process the same events
	DirAccess.remove_absolute(EVENTS_FILE)

	if text.is_empty():
		return

	var parsed = JSON.parse_string(text)
	if parsed == null or not parsed is Array:
		return

	for event in parsed:
		if event is Dictionary:
			chat_event.emit(event)
