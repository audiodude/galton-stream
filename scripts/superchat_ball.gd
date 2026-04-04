extends RigidBody2D

var color: Color = Color.WHITE
var ball_radius: float = 10.0
var initials: String = ""
var glowing: bool = true
var glow_phase: float = 0.0

func _draw():
	# Glow effect
	if glowing:
		var glow_alpha = 0.3 + 0.15 * sin(glow_phase * 4.0)
		draw_circle(Vector2.ZERO, ball_radius * 2.5, Color(color.r, color.g, color.b, glow_alpha * 0.4))
		draw_circle(Vector2.ZERO, ball_radius * 1.8, Color(color.r, color.g, color.b, glow_alpha * 0.6))

	# Main ball
	draw_circle(Vector2.ZERO, ball_radius, color)

	# Bright highlight
	draw_circle(Vector2(-2, -2), ball_radius * 0.35, Color(1, 1, 1, 0.4))

	# Draw initials
	if initials.length() > 0:
		var font = ThemeDB.fallback_font
		var font_size = int(ball_radius * 1.2)
		var text_size = font.get_string_size(initials, HORIZONTAL_ALIGNMENT_CENTER, -1, font_size)
		font.draw_string(get_canvas_item(),
			Vector2(-text_size.x / 2.0, text_size.y / 4.0),
			initials, HORIZONTAL_ALIGNMENT_CENTER, -1, font_size,
			Color(0, 0, 0, 0.7))

var settle_timer: float = 0.0

func _physics_process(delta):
	glow_phase += delta
	queue_redraw()

	if global_position.y > 1200 or global_position.x < -100 or global_position.x > 2020:
		queue_free()
		return

	# Freeze once settled in a bin
	if linear_velocity.length() < 5.0:
		settle_timer += delta
		if settle_timer > 1.0:
			stop_glow()
			freeze = true
			set_physics_process(false)
	else:
		settle_timer = 0.0

func stop_glow():
	glowing = false
	queue_redraw()
