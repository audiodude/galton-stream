extends Node2D

# Board geometry
const PEG_ROWS := 12
const PEG_SPACING_X := 110.0
const PEG_SPACING_Y := 60.0
const PEG_RADIUS := 10.0
const BALL_RADIUS := 7.0
const BALL_BOUNCE := 0.45
const BALL_FRICTION := 0.15
const SPAWN_INTERVAL := 0.18
var balls_per_round: int = 500
const MAX_ACTIVE_BALLS := 600
const BIN_COUNT := PEG_ROWS + 1

# Colors
const BG_COLOR := Color(0.05, 0.05, 0.12)
const PEG_COLOR := Color(0.55, 0.6, 0.75)
const WALL_COLOR := Color(0.3, 0.32, 0.45)

# Computed layout
var board_width: float
var board_offset_x: float
var peg_top_y: float = 160.0
var bin_top_y: float
var bin_bottom_y: float = 1080.0

var bin_counts: Array[int] = []
var bin_colors: Array[Color] = []  # Accumulated color per bin (RGB sum)
var total_dropped: int = 0
var round_dropped: int = 0
var cycle_count: int = 1
var color_phase: float = 0.0
var is_resetting: bool = false
var waiting_for_drain: bool = false
var drain_timer: float = 0.0
var current_colors: Array[Color] = []

# Spawn rate oscillation
var rate_target: float = 1.0
var rate_current: float = 1.0
var rate_velocity: float = 0.0
var rate_jump_timer: float = 0.0
const RATE_JUMP_INTERVAL := 4.0  # seconds between jumps
const RATE_DAMPING := 3.0
const RATE_STIFFNESS := 40.0

const ALL_COLORS := [
	Color(1.00, 0.20, 0.20),  # Red
	Color(0.15, 0.85, 1.00),  # Cyan
	Color(1.00, 0.95, 0.15),  # Yellow
	Color(0.55, 0.20, 1.00),  # Purple
	Color(1.00, 0.40, 0.10),  # Orange
	Color(0.10, 1.00, 0.45),  # Green
	Color(1.00, 0.20, 0.55),  # Hot pink
	Color(0.20, 0.40, 1.00),  # Blue
	Color(1.00, 0.65, 0.80),  # Salmon
	Color(0.00, 1.00, 0.85),  # Teal
	Color(0.90, 0.70, 0.10),  # Gold
	Color(0.75, 0.55, 1.00),  # Lavender
]
const COLOR_GROUP_SIZE := 12  # Balls per color group before shifting

@onready var spawn_timer: Timer = $SpawnTimer
@onready var reset_timer: Timer = $ResetTimer
@onready var pegs_node: Node2D = $Pegs
@onready var balls_node: Node2D = $Balls
@onready var histogram: Node2D = $Histogram
@onready var stats_label: Label = $StatsLabel
@onready var total_label: Label = $TotalLabel
@onready var title_label: Label = $TitleLabel
@onready var chat_reader: Node = $ChatReader
@onready var chat_display: Node2D = $ChatDisplay

var ball_script = preload("res://scripts/ball.gd")
var sticker_script = preload("res://scripts/sticker_effect.gd")
var superchat_script = preload("res://scripts/superchat_effect.gd")
var superchat_side: int = 1  # Alternates between -1 (left) and 1 (right)
var peg_script = preload("res://scripts/peg.gd")
var bin_areas: Array[Area2D] = []

func _ready():
	# Position window: origin for headless/Docker, offset for local dev
	if OS.has_environment("DISPLAY") and OS.get_environment("DISPLAY") == ":99":
		DisplayServer.window_set_position(Vector2i(0, 0))
	else:
		DisplayServer.window_set_position(Vector2i(1200, 150))

	# Cap FPS via env var (default 30 in headless, uncapped locally)
	if OS.has_environment("MAX_FPS"):
		Engine.max_fps = int(OS.get_environment("MAX_FPS"))
	elif OS.has_environment("DISPLAY") and OS.get_environment("DISPLAY") == ":99":
		Engine.max_fps = 30

	# Calculate board dimensions
	board_width = PEG_ROWS * PEG_SPACING_X
	board_offset_x = (1920.0 - board_width) / 2.0
	bin_top_y = peg_top_y + PEG_ROWS * PEG_SPACING_Y + 20

	# Init bin counts and colors
	bin_counts.resize(BIN_COUNT)
	bin_counts.fill(0)
	bin_colors.resize(BIN_COUNT)
	bin_colors.fill(Color.BLACK)
	_pick_round_colors()

	_create_pegs()
	_create_walls()
	_create_bin_walls()
	_create_bin_areas()
	_setup_labels()
	chat_reader.chat_event.connect(_on_chat_event)

	spawn_timer.wait_time = SPAWN_INTERVAL
	spawn_timer.timeout.connect(_on_spawn)
	spawn_timer.start()

	reset_timer.one_shot = true
	reset_timer.timeout.connect(_on_reset)

func _process(delta):
	if Input.is_action_just_pressed("ui_cancel"):
		get_tree().quit()
	color_phase += delta * 0.35
	_update_stats()
	_update_spawn_rate(delta)

	if waiting_for_drain:
		drain_timer += delta
		var last_peg_y = peg_top_y + (PEG_ROWS - 1) * PEG_SPACING_Y
		var all_past = true
		for ball in balls_node.get_children():
			if is_instance_valid(ball) and ball.global_position.y < last_peg_y:
				all_past = false
				break
		# Also wait for any active superchat effects to finish spawning
		var superchat_active = false
		for child in get_children():
			if child is Node2D and "active" in child and child.active:
				superchat_active = false  # Don't block on spawning, just on balls in flight
				break
		if (all_past and not superchat_active) or drain_timer > 10.0:
			waiting_for_drain = false
			drain_timer = 0.0
			_start_reset()

func _create_pegs():
	for row in range(PEG_ROWS):
		var num_pegs = row + 2
		var row_width = (num_pegs - 1) * PEG_SPACING_X
		var start_x = (1920.0 - row_width) / 2.0
		var y = peg_top_y + row * PEG_SPACING_Y

		for col in range(1, num_pegs - 1):
			var x = start_x + col * PEG_SPACING_X
			_create_peg(Vector2(x, y))

func _create_peg(pos: Vector2):
	var peg = StaticBody2D.new()
	peg.set_script(peg_script)
	peg.position = pos
	peg.radius = PEG_RADIUS

	var shape = CircleShape2D.new()
	shape.radius = PEG_RADIUS
	var col = CollisionShape2D.new()
	col.shape = shape
	peg.add_child(col)

	# Physics material for pegs
	var mat = PhysicsMaterial.new()
	mat.bounce = 0.3
	mat.friction = 0.1
	peg.physics_material_override = mat

	pegs_node.add_child(peg)

func _create_walls():
	var walls = $Walls as StaticBody2D
	var center_x = 960.0
	var pad = 25.0
	var top_half = PEG_SPACING_X * 0.5 + pad
	var top_y = peg_top_y - 30

	# Convex curved walls: start near top pegs, bulge outward, end at far bin edges
	var left_points = _make_convex_wall(-1, center_x, top_half, top_y)
	var right_points = _make_convex_wall(1, center_x, top_half, top_y)

	for i in range(left_points.size() - 1):
		_add_segment(walls, left_points[i], left_points[i + 1])
		_add_segment(walls, right_points[i], right_points[i + 1])

	# Floor
	_add_segment(walls, Vector2(board_offset_x, bin_bottom_y), Vector2(board_offset_x + board_width, bin_bottom_y))
	# Funnel at top
	_add_segment(walls, Vector2(center_x - 60, 30), Vector2(center_x - 15, top_y))
	_add_segment(walls, Vector2(center_x + 60, 30), Vector2(center_x + 15, top_y))

func _make_convex_wall(side: int, center_x: float, top_half: float, top_y: float) -> Array[Vector2]:
	# side: -1 for left, +1 for right
	# Cubic bezier: starts at top peg edge, curves outward, arrives vertically at bin edge
	var pad = 60.0
	var bin_edge_x = center_x + side * board_width * 0.5

	# P0: top, wider than first row pegs so balls don't escape
	var p0 = Vector2(center_x + side * (PEG_SPACING_X * 0.5 + pad), top_y)
	# P3: arrives at outer bin edge, at bin_top_y (then straight down)
	var p3 = Vector2(bin_edge_x, bin_top_y)

	# P1: controls the outward bulge direction from the top
	var bulge = 180.0
	var p1 = Vector2(p0.x + side * bulge, lerp(top_y, bin_top_y, 0.45))

	# P2: controls arrival — must be directly above P3 for vertical tangent
	var p2 = Vector2(bin_edge_x, lerp(top_y, bin_top_y, 0.55))

	# Sample the cubic bezier
	var segments := 30
	var points: Array[Vector2] = []
	for i in range(segments + 1):
		var t = float(i) / segments
		points.append(_bezier3(p0, p1, p2, p3, t))

	# Continue straight down the bin edge to the floor
	points.append(Vector2(bin_edge_x, bin_bottom_y))
	return points

func _bezier3(p0: Vector2, p1: Vector2, p2: Vector2, p3: Vector2, t: float) -> Vector2:
	var u = 1.0 - t
	return u*u*u*p0 + 3.0*u*u*t*p1 + 3.0*u*t*t*p2 + t*t*t*p3

func _create_bin_walls():
	var bin_walls_node = $BinWalls
	var bin_width = board_width / BIN_COUNT

	for i in range(BIN_COUNT + 1):
		var x = board_offset_x + i * bin_width
		var wall = StaticBody2D.new()
		# Full height collision
		var wall_start_y = bin_top_y
		_add_segment(wall, Vector2(x, wall_start_y), Vector2(x, bin_bottom_y))
		bin_walls_node.add_child(wall)

func _create_bin_areas():
	var bin_width = board_width / BIN_COUNT

	for i in range(BIN_COUNT):
		var area = Area2D.new()
		var bin_height = bin_bottom_y - bin_top_y
		area.position = Vector2(board_offset_x + i * bin_width + bin_width * 0.5, bin_top_y + bin_height * 0.5)

		var shape = RectangleShape2D.new()
		shape.size = Vector2(bin_width - 4, bin_height)
		var col = CollisionShape2D.new()
		col.shape = shape
		area.add_child(col)

		var idx = i
		area.body_entered.connect(func(body): _on_ball_binned(idx, body))
		$BinAreas.add_child(area)
		bin_areas.append(area)

func _add_segment(parent: Node2D, from: Vector2, to: Vector2):
	var shape = SegmentShape2D.new()
	shape.a = from
	shape.b = to
	var col = CollisionShape2D.new()
	col.shape = shape
	parent.add_child(col)

func _on_spawn():
	if is_resetting:
		return
	if balls_node.get_child_count() >= MAX_ACTIVE_BALLS:
		return

	var ball = RigidBody2D.new()
	ball.set_script(ball_script)
	ball.position = Vector2(960 + randf_range(-12, 12), 20)
	ball.ball_radius = BALL_RADIUS

	var shape = CircleShape2D.new()
	shape.radius = BALL_RADIUS
	var col = CollisionShape2D.new()
	col.shape = shape
	ball.add_child(col)

	var mat = PhysicsMaterial.new()
	mat.bounce = BALL_BOUNCE
	mat.friction = BALL_FRICTION
	ball.physics_material_override = mat
	ball.continuous_cd = RigidBody2D.CCD_MODE_CAST_RAY
	ball.linear_velocity = Vector2(randf_range(-15, 15), randf_range(5, 40))

	# Cycle through round colors with slight variation
	var base_color = current_colors[round_dropped % current_colors.size()]
	ball.color = Color(
		clampf(base_color.r + randf_range(-0.06, 0.06), 0.0, 1.0),
		clampf(base_color.g + randf_range(-0.06, 0.06), 0.0, 1.0),
		clampf(base_color.b + randf_range(-0.06, 0.06), 0.0, 1.0)
	)

	balls_node.add_child(ball)
	total_dropped += 1
	round_dropped += 1
	if round_dropped >= balls_per_round:
		spawn_timer.stop()
		waiting_for_drain = true

func _on_ball_binned(bin_index: int, body: Node2D):
	if not body is RigidBody2D:
		return
	if is_resetting:
		return
	if bin_index < 0 or bin_index >= BIN_COUNT:
		return

	# Get ball color and blend into bin
	var ball_color := Color.WHITE
	if body.has_method("_draw") and "color" in body:
		ball_color = body.color

	bin_counts[bin_index] += 1
	# Running average: blend new ball color into existing bin color
	var n = bin_counts[bin_index]
	var prev = bin_colors[bin_index]
	bin_colors[bin_index] = Color(
		(prev.r * (n - 1) + ball_color.r) / n,
		(prev.g * (n - 1) + ball_color.g) / n,
		(prev.b * (n - 1) + ball_color.b) / n
	)

	histogram.update_data(bin_counts, bin_colors, board_offset_x, board_width, bin_top_y)


func _start_reset():
	if is_resetting:
		return
	is_resetting = true
	round_dropped = 0
	spawn_timer.stop()
	reset_timer.wait_time = 2.5
	reset_timer.start()

func _on_reset():
	# Disable bin detection during reset
	for area in bin_areas:
		area.monitoring = false

	var children = balls_node.get_children()
	if not children.is_empty():
		var tween = create_tween()
		tween.set_parallel(true)
		for ball in children:
			if is_instance_valid(ball):
				tween.tween_property(ball, "scale", Vector2.ZERO, 0.4)
		tween.tween_property(histogram, "fade", 0.0, 0.4)
		await tween.finished

	# Free all balls
	for ball in balls_node.get_children():
		if is_instance_valid(ball):
			ball.queue_free()

	# Wait for queue_free to complete
	await get_tree().process_frame
	await get_tree().process_frame

	# Clean up superchat effects and ramps
	for child in get_children():
		if child.has_method("_cleanup"):
			child._cleanup()

	# Reset state
	bin_counts.fill(0)
	bin_colors.fill(Color.BLACK)
	histogram.fade = 1.0
	histogram.update_data(bin_counts, bin_colors, board_offset_x, board_width, bin_top_y)
	cycle_count += 1
	_pick_round_colors()

	# Re-enable bin detection
	for area in bin_areas:
		area.monitoring = true

	is_resetting = false
	spawn_timer.start()

func _update_spawn_rate(delta):
	# Jump to new target periodically
	rate_jump_timer -= delta
	if rate_jump_timer <= 0:
		rate_target = randf_range(0.6, 1.4)
		rate_jump_timer = RATE_JUMP_INTERVAL

	# Damped spring toward target
	var force = RATE_STIFFNESS * (rate_target - rate_current) - RATE_DAMPING * rate_velocity
	rate_velocity += force * delta
	rate_current += rate_velocity * delta

	spawn_timer.wait_time = SPAWN_INTERVAL / rate_current

func _pick_round_colors():
	balls_per_round = randi_range(472, 512)
	var shuffled = ALL_COLORS.duplicate()
	shuffled.shuffle()
	var count = 2 if randf() < 0.5 else 3
	current_colors = []
	for i in range(count):
		current_colors.append(shuffled[i])

func _setup_labels():
	title_label.visible = false

	var label_color = Color(0.6, 0.65, 0.8, 0.7)

	# Round count + divider (right-aligned, fixed position)
	stats_label.add_theme_font_size_override("font_size", 32)
	stats_label.add_theme_color_override("font_color", label_color)
	stats_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	stats_label.position = Vector2(1400, 10)
	stats_label.size = Vector2(300, 60)

	# Total count (left-aligned, right of divider)
	total_label.add_theme_font_size_override("font_size", 32)
	total_label.add_theme_color_override("font_color", label_color)
	total_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_LEFT
	total_label.position = Vector2(1720, 10)
	total_label.size = Vector2(180, 60)

func _update_stats():
	stats_label.text = "%d  |" % round_dropped
	total_label.text = "%d" % total_dropped

func _draw():
	var center_x = 960.0
	var pad = 25.0
	var top_half = PEG_SPACING_X * 0.5 + pad
	var top_y = peg_top_y - 30
	var wall_thick = 5.0

	# Draw funnel
	draw_line(Vector2(center_x - 60, 30), Vector2(center_x - 15, top_y), WALL_COLOR, wall_thick)
	draw_line(Vector2(center_x + 60, 30), Vector2(center_x + 15, top_y), WALL_COLOR, wall_thick)

	# Draw convex curved walls
	var left_points = _make_convex_wall(-1, center_x, top_half, top_y)
	var right_points = _make_convex_wall(1, center_x, top_half, top_y)
	for i in range(left_points.size() - 1):
		draw_line(left_points[i], left_points[i + 1], WALL_COLOR, wall_thick)
		draw_line(right_points[i], right_points[i + 1], WALL_COLOR, wall_thick)

	# Draw bin walls (thick visual dividers)
	var bin_width = board_width / BIN_COUNT
	for i in range(BIN_COUNT + 1):
		var x = board_offset_x + i * bin_width
		draw_line(Vector2(x, bin_top_y), Vector2(x, bin_bottom_y), WALL_COLOR, wall_thick)

func _on_chat_event(event: Dictionary):
	var event_type = event.get("type", "")
	var name = event.get("name", "")
	var text = ""
	var color = Color.WHITE

	match event_type:
		"join":
			text = "Welcome %s!" % name
			color = current_colors[randi() % current_colors.size()]
		"welcome_back":
			text = "Welcome back %s!" % name
			color = current_colors[randi() % current_colors.size()]
		"gift":
			_spawn_superchat(name, event.get("amount", "$1"))
			return
		"sticker":
			_spawn_sticker_effect(name, event.get("amount", ""))
			return
		_:
			return

	if text.is_empty():
		return

	var container = HBoxContainer.new()
	container.position = Vector2(40, 500 + chat_display.get_child_count() * 40)

	if event_type == "join" or event_type == "welcome_back":
		var welcome_label = Label.new()
		welcome_label.text = "Welcome back " if event_type == "welcome_back" else "Welcome "
		welcome_label.add_theme_font_size_override("font_size", 28)
		welcome_label.add_theme_color_override("font_color", current_colors[randi() % current_colors.size()])
		container.add_child(welcome_label)

		var name_label = Label.new()
		name_label.text = name + "!"
		name_label.add_theme_font_size_override("font_size", 28)
		var name_color = current_colors[randi() % current_colors.size()]
		while name_color == welcome_label.get_theme_color("font_color") and current_colors.size() > 1:
			name_color = current_colors[randi() % current_colors.size()]
		name_label.add_theme_color_override("font_color", name_color)
		container.add_child(name_label)
	else:
		var label = Label.new()
		label.text = text
		label.add_theme_font_size_override("font_size", 28)
		label.add_theme_color_override("font_color", color)
		container.add_child(label)

	chat_display.add_child(container)

	# Scroll up and fade out over 12 seconds
	var tween = create_tween()
	tween.set_parallel(true)
	tween.tween_property(container, "position:y", container.position.y - 300, 12.0)
	tween.tween_property(container, "modulate:a", 0.0, 12.0).set_ease(Tween.EASE_IN).set_trans(Tween.TRANS_EXPO)
	tween.set_parallel(false)
	tween.tween_callback(container.queue_free)

func _spawn_sticker_effect(user_name: String, amount: String):
	var sticker = RigidBody2D.new()
	sticker.set_script(sticker_script)

	# Start from above the screen, left or right side
	var from_left = randf() < 0.5
	if from_left:
		sticker.position = Vector2(-200, randf_range(-200, -50))
		sticker.linear_velocity = Vector2(randf_range(350, 500), randf_range(30, 80))
	else:
		sticker.position = Vector2(2120, randf_range(-200, -50))
		sticker.linear_velocity = Vector2(randf_range(-500, -350), randf_range(30, 80))

	var typed_colors: Array[Color] = []
	for c in current_colors:
		typed_colors.append(c)
	sticker.setup(user_name, amount, typed_colors)
	sticker.z_index = 20

	# Add to the main scene so it can interact with walls
	add_child(sticker)

func _spawn_superchat(user_name: String, amount_str: String):
	# Parse dollar amount
	var dollars = int(amount_str.replace("$", "").replace(",", "").strip_edges())
	if dollars <= 0:
		dollars = 1

	# Pick a color from the current palette
	var color = current_colors[randi() % current_colors.size()]

	# Alternate sides
	superchat_side *= -1

	var effect = Node2D.new()
	effect.set_script(superchat_script)

	var typed_bin_areas: Array[Area2D] = []
	for a in bin_areas:
		typed_bin_areas.append(a)
	effect.setup(user_name, dollars, color, superchat_side, typed_bin_areas)

	add_child(effect)
