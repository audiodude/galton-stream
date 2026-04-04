extends Node2D

## Spawns a side funnel with username and drops glowing balls into the board.
## One ball per dollar.

var display_name: String = ""
var amount_dollars: int = 1
var ball_color: Color = Color.WHITE
var initials: String = ""
var side: int = 1  # -1 = left, 1 = right
var ramp_start: Vector2 = Vector2.ZERO
var ramp_end: Vector2 = Vector2.ZERO
var ramp_body: StaticBody2D = null
var balls_spawned: int = 0
var spawn_timer: float = 0.0
var spawn_interval: float = 0.15
var active: bool = true
var name_alpha: float = 1.0

var superchat_ball_script = preload("res://scripts/superchat_ball.gd")

# References to bin areas for glow detection
var bin_areas: Array[Area2D] = []

func setup(p_name: String, p_dollars: int, p_color: Color, p_side: int, p_bin_areas: Array[Area2D]):
	display_name = p_name
	amount_dollars = p_dollars
	ball_color = p_color
	side = p_side
	bin_areas = p_bin_areas

	# Get initials: first and last character of name
	var clean_name = p_name.strip_edges()
	if clean_name.length() >= 2:
		initials = clean_name[0].to_upper() + clean_name[clean_name.length() - 1].to_upper()
	elif clean_name.length() == 1:
		initials = clean_name[0].to_upper()

	# Ramp: physical angled line that balls bounce off and slide into the board
	# Left ramp: from upper-left area down toward center
	# Right ramp: from upper-right area down toward center
	if side == -1:
		ramp_start = Vector2(550, 15)
		ramp_end = Vector2(820, 100)
	else:
		ramp_start = Vector2(1370, 15)
		ramp_end = Vector2(1100, 100)

func _ready():
	z_index = 15

	# Create physical ramp that balls bounce off
	ramp_body = StaticBody2D.new()
	var shape = SegmentShape2D.new()
	shape.a = ramp_start
	shape.b = ramp_end
	var col = CollisionShape2D.new()
	col.shape = shape
	ramp_body.add_child(col)

	var mat = PhysicsMaterial.new()
	mat.bounce = 0.3
	mat.friction = 0.1
	ramp_body.physics_material_override = mat

	get_parent().add_child.call_deferred(ramp_body)

func _process(delta):
	if not active:
		return

	spawn_timer -= delta
	if spawn_timer <= 0 and balls_spawned < amount_dollars:
		spawn_timer = spawn_interval
		_spawn_ball()
		balls_spawned += 1

		if balls_spawned >= amount_dollars:
			active = false
			# Fade out name after all balls dropped
			var tween = create_tween()
			tween.tween_interval(3.0)
			tween.tween_property(self, "name_alpha", 0.0, 2.0)
			tween.tween_callback(_cleanup)

	queue_redraw()

func _draw():
	# Draw the ramp
	var ramp_color = Color(ball_color.r, ball_color.g, ball_color.b, name_alpha * 0.9)
	draw_line(ramp_start, ramp_end, ramp_color, 4.0)

	# Username and amount beside the top of the ramp
	var font = ThemeDB.fallback_font
	var name_size = 22
	var amount_size = 18
	var name_color = Color(ball_color.r, ball_color.g, ball_color.b, name_alpha)
	var amount_color = Color(1.0, 1.0, 1.0, name_alpha * 0.8)

	var name_text = "@%s" % display_name
	var amount_text = "$%d" % amount_dollars

	# Text at the outer end of the ramp (top), centered
	var name_width = font.get_string_size(name_text, HORIZONTAL_ALIGNMENT_LEFT, -1, name_size).x
	var amount_width = font.get_string_size(amount_text, HORIZONTAL_ALIGNMENT_LEFT, -1, amount_size).x

	# Draw text below the ramp, rotated to match its angle
	var ramp_dir = (ramp_end - ramp_start).normalized()
	var ramp_angle = ramp_dir.angle()
	var ramp_mid = (ramp_start + ramp_end) / 2.0

	# For right side, flip the direction so text reads left-to-right
	if side == 1:
		ramp_angle += PI

	# Offset below the ramp (perpendicular, always downward)
	var perp = Vector2(-ramp_dir.y, ramp_dir.x) * 30.0
	if side == 1:
		perp = -perp
	var text_origin = ramp_mid + perp

	draw_set_transform(text_origin, ramp_angle)
	font.draw_string(get_canvas_item(),
		Vector2(-name_width / 2.0, 0),
		name_text, HORIZONTAL_ALIGNMENT_LEFT, -1, name_size, name_color)
	font.draw_string(get_canvas_item(),
		Vector2(-amount_width / 2.0, 22),
		amount_text, HORIZONTAL_ALIGNMENT_LEFT, -1, amount_size, amount_color)
	draw_set_transform(Vector2.ZERO, 0)

func _spawn_ball():
	var ball = RigidBody2D.new()
	ball.set_script(superchat_ball_script)
	# Spawn above the middle of the ramp
	var spawn_x = lerp(ramp_start.x, ramp_end.x, 0.3) + randf_range(-10, 10)
	var spawn_y = ramp_start.y - 20
	ball.position = Vector2(spawn_x, spawn_y)
	ball.ball_radius = 10.0
	ball.color = ball_color
	ball.initials = initials

	var shape = CircleShape2D.new()
	shape.radius = 10.0
	var col = CollisionShape2D.new()
	col.shape = shape
	ball.add_child(col)

	var mat = PhysicsMaterial.new()
	mat.bounce = 0.45
	mat.friction = 0.15
	ball.physics_material_override = mat
	ball.continuous_cd = RigidBody2D.CCD_MODE_CAST_RAY
	ball.z_index = 15

	# Just drop, let the ramp guide them
	ball.linear_velocity = Vector2(randf_range(-10, 10), randf_range(10, 40))

	get_parent().add_child(ball)

	# Count in the main stats
	var main = get_parent()
	if main.has_method("_update_stats"):
		main.total_dropped += 1
		main.round_dropped += 1

	# Connect to bin areas to stop glow on landing
	for area in bin_areas:
		var ball_ref = ball
		area.body_entered.connect(func(body):
			if body == ball_ref:
				ball_ref.stop_glow()
		)

func _cleanup():
	if is_instance_valid(ramp_body):
		ramp_body.queue_free()
	queue_free()
