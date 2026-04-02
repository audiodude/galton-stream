extends StaticBody2D

var base_color: Color = Color(0.55, 0.6, 0.75)
var flash_color: Color = Color(0.85, 0.9, 1.0)
var flash_amount: float = 0.0
var radius: float = 10.0

func _ready():
	# Add a slightly larger Area2D to detect ball touches
	var area = Area2D.new()
	var shape = CircleShape2D.new()
	shape.radius = radius + 4.0
	var col = CollisionShape2D.new()
	col.shape = shape
	area.add_child(col)
	area.body_entered.connect(_on_hit)
	add_child(area)

func _on_hit(_body):
	flash_amount = 1.0

func _process(delta):
	if flash_amount > 0.0:
		flash_amount = max(flash_amount - delta * 3.0, 0.0)
		queue_redraw()

func _draw():
	var color = base_color.lerp(flash_color, flash_amount)
	draw_circle(Vector2.ZERO, radius, color)
	draw_circle(Vector2(-2, -2), radius * 0.3, Color(0.8, 0.85, 0.95, 0.25))
