extends RigidBody2D

## Animated Super Sticker notification that falls, bounces off the board wall,
## spins, and explodes into confetti.

var display_name: String = ""
var amount: String = ""
var colors: Array[Color] = []
var font_size: int = 36
var has_collided: bool = false
var letter_colors: Array[Color] = []

func setup(p_name: String, p_amount: String, p_colors: Array[Color]):
	display_name = p_name
	amount = p_amount
	colors = p_colors

	# Pre-assign alternating colors to each letter across both lines
	var line1 = "Thanks! %s" % amount
	var line2 = "@%s" % display_name
	for i in range(line1.length() + line2.length()):
		letter_colors.append(colors[i % colors.size()])

func _ready():
	# Set up physics
	gravity_scale = 0.4
	mass = 5.0
	contact_monitor = true
	max_contacts_reported = 1

	# Collision shape sized to text
	var shape = RectangleShape2D.new()
	shape.size = Vector2(400, 60)
	var col = CollisionShape2D.new()
	col.shape = shape
	add_child(col)

	# Set up collision detection
	body_entered.connect(_on_body_entered)

	# Give it an initial angular push
	var side = sign(position.x - 960)
	angular_velocity = side * randf_range(0.3, 0.8)

func _draw():
	var line1 = "Thanks! %s" % amount
	var line2 = "@%s" % display_name
	var font = ThemeDB.fallback_font
	var color_idx = 0

	var small_size = int(font_size * 0.7)

	# Measure widths for centering
	var line1_width = font.get_string_size(line1, HORIZONTAL_ALIGNMENT_LEFT, -1, small_size).x
	var line2_width = font.get_string_size(line2, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size).x

	# Draw line 1 (white, smaller, centered)
	var x_offset = -line1_width / 2.0
	for i in range(line1.length()):
		var ch = line1[i]
		font.draw_string(get_canvas_item(), Vector2(x_offset, -font_size * 0.3),
			ch, HORIZONTAL_ALIGNMENT_LEFT, -1, small_size, Color.WHITE)
		x_offset += font.get_string_size(ch, HORIZONTAL_ALIGNMENT_LEFT, -1, small_size).x

	# Draw line 2 (colored, larger, centered)
	x_offset = -line2_width / 2.0
	for i in range(line2.length()):
		var ch = line2[i]
		var color = letter_colors[color_idx] if color_idx < letter_colors.size() else Color.WHITE
		font.draw_string(get_canvas_item(), Vector2(x_offset, font_size * 0.6),
			ch, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size, color)
		x_offset += font.get_string_size(ch, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size).x
		color_idx += 1

func _on_body_entered(_body):
	if has_collided:
		return
	has_collided = true

	# Spin faster on impact
	angular_velocity = angular_velocity * 4.0
	apply_central_impulse(Vector2(0, -300))

	# Wait a beat then explode
	await get_tree().create_timer(1.75).timeout
	_explode()

func _explode():
	var parent = get_parent()
	var pos = global_position

	# Spawn colored confetti
	for i in range(40):
		var confetti = _make_confetti(pos, colors[i % colors.size()])
		parent.add_child(confetti)

	# Splash of white confetti
	for i in range(15):
		var confetti = _make_confetti(pos, Color.WHITE)
		parent.add_child(confetti)

	# Fade out the text
	var tween = create_tween()
	tween.tween_property(self, "modulate:a", 0.0, 0.3)
	tween.tween_callback(queue_free)

func _make_confetti(pos: Vector2, color: Color) -> RigidBody2D:
	var piece = RigidBody2D.new()
	piece.position = pos + Vector2(randf_range(-50, 50), randf_range(-30, 30))
	piece.gravity_scale = 0.6
	piece.mass = 0.5
	piece.linear_damp = 0.5

	# Random velocity burst
	piece.linear_velocity = Vector2(randf_range(-400, 400), randf_range(-600, -100))
	piece.angular_velocity = randf_range(-10, 10)

	# Tiny collision shape
	var shape = CircleShape2D.new()
	shape.radius = 4.0
	var col = CollisionShape2D.new()
	col.shape = shape
	piece.add_child(col)

	# Draw as a small colored rectangle
	var script_text = """extends RigidBody2D
var color: Color
var size: Vector2
func _draw():
	draw_rect(Rect2(-size/2, size), color)
"""
	# Instead, use a simple approach with a custom draw
	piece.set_meta("confetti_color", color)
	piece.set_meta("confetti_size", Vector2(randf_range(4, 10), randf_range(4, 10)))
	piece.set_script(ConfettiPiece)

	return piece

# Inner class for confetti rendering
class ConfettiPiece extends RigidBody2D:
	var confetti_color: Color = Color.WHITE
	var confetti_size: Vector2 = Vector2(6, 6)
	var lifetime: float = 0.0

	func _ready():
		confetti_color = get_meta("confetti_color", Color.WHITE)
		confetti_size = get_meta("confetti_size", Vector2(6, 6))

	func _draw():
		draw_rect(Rect2(-confetti_size / 2, confetti_size), confetti_color)

	func _process(delta):
		lifetime += delta
		if lifetime > 4.0:
			modulate.a -= delta * 2.0
			if modulate.a <= 0:
				queue_free()
		if global_position.y > 1200:
			queue_free()
