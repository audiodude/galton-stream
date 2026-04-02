extends RigidBody2D

var color: Color = Color.WHITE
var ball_radius: float = 8.0

func _draw():
	draw_circle(Vector2.ZERO, ball_radius, color)
	# Subtle highlight
	draw_circle(Vector2(-2, -2), ball_radius * 0.35, Color(1, 1, 1, 0.25))

func _physics_process(_delta):
	if global_position.y > 1200 or global_position.x < -100 or global_position.x > 2020:
		queue_free()
