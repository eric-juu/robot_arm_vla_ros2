"""
A small tabletop world for the simulated arm: forward kinematics from the K1's URDF, a pinhole wrist camera at
`usb_cam_link`, a renderer (Pillow, CPU only), and just enough contact physics for the eval's tasks.

    scene = Scene.from_spec({"base": "bench"})
    jpeg = scene.render(joints, gripper)            # what the wrist camera sees
    scene.in_view("red_block", joints)              # where an object lands in the image

Objects (positions in metres, in the arm's base frame: x forward, y left, z up, table top at z = 0):
    block    a box that can be pushed into, grasped and lifted     {xyz, size: [x, y, z], color}
    board    a flat plate (a dev board); solid                       {xyz, size, color}
    button   a push button on a board; counts presses                {xyz, size, color}
    led      a light that is on or off                               {xyz, color, on}
    display  a 7-segment style readout                               {xyz, size: [x, y], text, sequence}
    note     a sheet lying flat with text on it                      {xyz, size: [x, y], text}
    hand     a person's hand held up in the workspace (a hazard)     {xyz, size: [w, h], follow_camera}
    mark     a tape mark on the table                                {xyz, color, label}

Contact: a pose that would push the fingertips or the gripper into the table or a solid object is not taken
(the arm stops short, so the move ends `not_reached`), a fingertip entering a button from above presses it, and
closing the gripper around a block holds it until it opens again.

A reset press reboots the board: a display counts boots, or with `sequence: [...]` shows the next entry (a boot
code) after each one. An object with `follow_camera: d` (metres) stays d in front of the wrist camera, like a
hand reaching in front of the arm wherever it looks.

The kinematic constants below are copied from ros2/k1_description/urdf/k1_standalone.urdf (a test checks they
still match), so the agent doesn't need the ROS workspace.
"""
import copy
import io
import math

import yaml

# (origin xyz, axis) of joint1..joint6, each relative to the previous link (URDF rpy is 0 for all of them)
CHAIN = [((0.045, 0.0, 0.07), (0, 0, -1)),
         ((0.0, -0.0001, 0.055), (0, -1, 0)),
         ((0.033, -0.0001, 0.179), (0, -1, 0)),
         ((0.0845, 0.0, 0.0), (-1, 0, 0)),
         ((0.08435, 0.00065, 0.0), (0, -1, 0)),
         ((0.061387, 0.0, 0.0), (-1, 0, 0))]
GRASP_OFFSET = (0.0557, 0.0, 0.0)               # link6 -> link_grasping_frame
CAMERA_OFFSET = (0.05, 0.0, 0.05825)            # link6 -> usb_cam_link (an optical frame: z forward, x right)
CAMERA_RPY = (-1.57, 0.0, -1.57)
FINGER_BASE = (0.0695, 0.033, 0.0)              # link6 -> link_gripper_left (right: y mirrored)
FINGER_LENGTH = 0.032
FINGER_TRAVEL = 0.034                           # per finger, open (0) to closed

WIDTH, HEIGHT = 640, 480
HFOV = math.radians(70.0)
FOCAL = (WIDTH / 2) / math.tan(HFOV / 2)
NEAR = 0.01

COLORS = {"red": (200, 40, 40), "blue": (40, 80, 200), "green": (40, 150, 70), "yellow": (230, 200, 40),
          "black": (30, 30, 30), "white": (235, 235, 235), "orange": (230, 120, 30), "pcb": (20, 110, 60),
          "grey": (120, 120, 120), "skin": (224, 172, 138)}
TABLE = (172, 150, 120)
WALL = (205, 205, 198)

SCENES = {
    "empty": [],
    # what the sim's "overview" pose sees from straight above; the red block is where a level gripper can grasp it
    "tabletop": [
        {"name": "mark_a", "kind": "mark", "xyz": [0.30, 0.0, 0.0], "color": "yellow", "label": "A"},
        {"name": "red_block", "kind": "block", "xyz": [0.30, 0.0, 0.0], "size": [0.03, 0.03, 0.03], "color": "red"},
        {"name": "blue_block", "kind": "block", "xyz": [0.28, 0.08, 0.0], "size": [0.03, 0.03, 0.03],
         "color": "blue"},
    ],
    "bench": [
        {"name": "board", "kind": "board", "xyz": [0.27, -0.01, 0.0], "size": [0.10, 0.08, 0.006], "color": "pcb"},
        {"name": "reset", "kind": "button", "xyz": [0.29, -0.03, 0.006], "size": [0.012, 0.012, 0.006],
         "color": "black"},
        {"name": "pwr", "kind": "led", "xyz": [0.25, 0.02, 0.006], "color": "green", "on": True},
        {"name": "stat", "kind": "led", "xyz": [0.25, 0.005, 0.006], "color": "orange", "on": False},
        {"name": "display", "kind": "display", "xyz": [0.29, 0.015, 0.006], "size": [0.03, 0.02], "text": "42"},
    ],
}


# ---------------- small linear algebra ----------------

def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def apply(r, v):
    return [r[0][0] * v[0] + r[0][1] * v[1] + r[0][2] * v[2],
            r[1][0] * v[0] + r[1][1] * v[1] + r[1][2] * v[2],
            r[2][0] * v[0] + r[2][1] * v[1] + r[2][2] * v[2]]


def transpose(r):
    return [[r[j][i] for j in range(3)] for i in range(3)]


def axis_angle(axis, angle):
    x, y, z = axis
    c, s, t = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c]]


def rpy(roll, pitch, yaw):
    return matmul(axis_angle((0, 0, 1), yaw), matmul(axis_angle((0, 1, 0), pitch), axis_angle((1, 0, 0), roll)))


class Pose:
    def __init__(self, r=None, t=None):
        self.r = r or [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        self.t = list(t or (0.0, 0.0, 0.0))

    def __mul__(self, other: "Pose") -> "Pose":
        return Pose(matmul(self.r, other.r), [a + b for a, b in zip(self.t, apply(self.r, other.t))])

    def point(self, v) -> list[float]:
        return [a + b for a, b in zip(self.t, apply(self.r, v))]


def forward(joints) -> dict:
    """World poses of link6, the grasping frame, the camera and the fingertips for 6 joint angles."""
    pose = Pose()
    for (xyz, axis), angle in zip(CHAIN, joints):
        pose = pose * Pose(t=xyz) * Pose(axis_angle(axis, angle))
    return {"link6": pose, "grasp": pose * Pose(t=GRASP_OFFSET), "camera": pose * Pose(rpy(*CAMERA_RPY), CAMERA_OFFSET)}


def finger_points(link6: Pose, gripper: float) -> list[list[float]]:
    """Inner fingertip points (left, right) for a gripper opening 1.0 (open) .. 0.0 (closed)."""
    inward = FINGER_TRAVEL * (1.0 - min(max(gripper, 0.0), 1.0))
    tip_x = FINGER_BASE[0] + FINGER_LENGTH
    return [link6.point((tip_x, FINGER_BASE[1] - inward, 0.0)), link6.point((tip_x, -FINGER_BASE[1] + inward, 0.0))]


def contact_points(joints, gripper: float) -> list[list[float]]:
    """Points that must stay out of the table and solid objects: the fingertips, the wrist and the gripper body
    (not the space between the fingers, where a grasped block goes)."""
    link6 = forward(joints)["link6"]
    return finger_points(link6, gripper) + [link6.t, link6.point((0.0, 0.0, -0.025)),
                                            link6.point((FINGER_BASE[0], 0.0, -0.012))]


# ---------------- the world ----------------

class Scene:

    def __init__(self, objects: list[dict]):
        self.objects = []
        for spec in objects:
            obj = copy.deepcopy(spec)
            obj.setdefault("name", f"{obj['kind']}{len(self.objects)}")
            obj["xyz"] = [float(v) for v in obj.get("xyz", [0.3, 0.0, 0.0])]
            obj.setdefault("presses", 0)
            self.objects.append(obj)
        self.held: dict | None = None           # {"name", "offset"} while a block is in the gripper
        self.regrasp_needs_open = False         # after a slip, the gripper must open before it can grasp again
        self.version = 0                        # bumps when anything visible changes

    @classmethod
    def from_spec(cls, spec) -> "Scene":
        """A preset name, a list of objects, or {"base": preset, "objects": [...]} (objects replace by name)."""
        if spec is None:
            return cls([])
        if isinstance(spec, str):
            spec = {"base": spec}
        if isinstance(spec, list):
            spec = {"objects": spec}
        objects = {o["name"]: o for o in copy.deepcopy(SCENES[spec.get("base", "empty")])}
        for o in spec.get("objects") or []:
            objects[o["name"]] = {**objects.get(o["name"], {}), **o}
        return cls([o for o in objects.values() if not o.get("remove")])

    def get(self, name: str) -> dict:
        for obj in self.objects:
            if obj["name"] == name:
                return obj
        raise KeyError(f"no object {name!r} in the scene; objects: {[o['name'] for o in self.objects]}")

    # ---- physics ----

    @staticmethod
    def _inside(point, obj, margin=0.0) -> bool:
        sx, sy, sz = obj["size"]
        x, y, z = obj["xyz"]
        return (abs(point[0] - x) <= sx / 2 + margin and abs(point[1] - y) <= sy / 2 + margin
                and z - margin <= point[2] <= z + sz + margin)

    def collides(self, joints, gripper: float) -> bool:
        """Would this pose push the gripper into the table or a solid object?"""
        for p in contact_points(joints, gripper):
            if p[2] < 0.002:
                return True
            for obj in self.objects:
                if obj["kind"] in ("block", "board") and (self.held is None or obj["name"] != self.held["name"]):
                    if self._inside(p, obj):
                        return True
                elif obj["kind"] == "button" and self._inside(p, obj):
                    sx, sy, sz = obj["size"]
                    if p[2] < obj["xyz"][2] + sz * 0.3:       # past the button's travel: hard stop
                        return True
        return False

    def step(self, joints, gripper: float):
        """Update presses and the held block after the arm moved to `joints` (already free of collisions)."""
        frames = forward(joints)
        tips = finger_points(frames["link6"], gripper) + [frames["grasp"].t]
        for obj in self.objects:
            if obj["kind"] == "button":
                inside = any(self._inside(p, obj) for p in tips)
                if inside and not obj.get("_down"):
                    obj["presses"] += 1
                    self.on_press(obj)
                    self.version += 1
                obj["_down"] = inside
        grasp = frames["grasp"]
        if gripper > 0.6:
            self.regrasp_needs_open = False
        if self.held is None and gripper < 0.5 and not self.regrasp_needs_open:
            for obj in self.objects:
                if obj["kind"] == "block":
                    local = apply(transpose(grasp.r), [a - b for a, b in zip(self.center(obj), grasp.t)])
                    if abs(local[0]) < obj["size"][0] / 2 + 0.01 and abs(local[1]) < 0.02 and abs(local[2]) < 0.02:
                        self.held = {"name": obj["name"], "offset": local}
                        self.version += 1
                        break
        elif self.held is not None and gripper > 0.6:
            obj = self.get(self.held["name"])
            obj["xyz"][2] = 0.0                     # it drops to the table
            self.held = None
            self.version += 1
        if self.held is not None:
            obj = self.get(self.held["name"])
            center = grasp.point(self.held["offset"])
            obj["xyz"] = [center[0], center[1], center[2] - obj["size"][2] / 2]
            self.version += 1

    def drop(self):
        """The held block slips out and falls to the table (it can be grasped again once the gripper opens)."""
        if self.held is not None:
            self.get(self.held["name"])["xyz"][2] = 0.0
            self.held = None
            self.regrasp_needs_open = True
            self.version += 1

    def gripper_stop(self, command: float) -> float:
        """Where the fingers actually stop: on the held block instead of closing fully."""
        if self.held is None:
            return command
        width = self.get(self.held["name"])["size"][1]
        return max(command, 1.0 - (FINGER_BASE[1] * 2 - width) / 2 / FINGER_TRAVEL)

    def on_press(self, button: dict):
        """A dev board's reset button reboots it: the status LED blinks and the display counts boots (or shows the
        next boot code of its `sequence`)."""
        for obj in self.objects:
            if obj["kind"] == "display" and obj.get("sequence"):
                obj["boots"] = obj.get("boots", 0) + 1
                obj["text"] = str(obj["sequence"][(obj["boots"] - 1) % len(obj["sequence"])])
            elif obj["kind"] == "display" and obj.get("counts_boots", True):
                text = str(obj.get("text", "0"))
                obj["text"] = str(int(text) + 1) if text.isdigit() else "1"
            if obj["kind"] == "led" and obj["name"] != "pwr":
                obj["on"] = not obj.get("on", False)

    @staticmethod
    def center(obj) -> list[float]:
        size = obj.get("size", [0.0, 0.0, 0.0])
        return [obj["xyz"][0], obj["xyz"][1], obj["xyz"][2] + (size[2] / 2 if len(size) > 2 else 0.0)]

    # ---- the camera ----

    def follow(self, camera: Pose):
        """Put objects with `follow_camera: d` d metres in front of the camera, centred on its view."""
        for obj in self.objects:
            if obj.get("follow_camera"):
                x, y, z = camera.point((0.0, 0.0, float(obj["follow_camera"])))
                h = obj.get("size", [0.09, 0.18])[1]
                obj["xyz"] = [x, y, max(0.0, z - h / 2)]

    @staticmethod
    def project(camera: Pose, point) -> tuple[float, float, float]:
        local = apply(transpose(camera.r), [a - b for a, b in zip(point, camera.t)])
        z = local[2]
        if z <= NEAR:
            return float("nan"), float("nan"), z
        return FOCAL * local[0] / z + WIDTH / 2, FOCAL * local[1] / z + HEIGHT / 2, z

    def in_view(self, name: str, joints) -> dict:
        """Where an object's centre lands in the image: {visible, u, v, region (3x3 grid name), distance}."""
        obj = self.get(name)
        u, v, z = self.project(forward(joints)["camera"], self.center(obj))
        visible = z > NEAR and 0 <= u < WIDTH and 0 <= v < HEIGHT
        region = None
        if visible:
            col = ["left", "centre", "right"][min(2, int(u / (WIDTH / 3)))]
            row = ["top", "middle", "bottom"][min(2, int(v / (HEIGHT / 3)))]
            region = "centre" if (col, row) == ("centre", "middle") else f"{row}-{col}"
        return {"visible": visible, "u": round(u, 1) if visible else None, "v": round(v, 1) if visible else None,
                "region": region, "distance": round(z, 3)}

    def render(self, joints, gripper: float = 1.0, quality: int = 80) -> bytes:
        from PIL import Image, ImageDraw

        frames = forward(joints)
        camera = frames["camera"]
        self.follow(camera)
        image = Image.new("RGB", (WIDTH, HEIGHT), (150, 150, 150))
        draw = ImageDraw.Draw(image)
        self._polygon(draw, camera, [(1.2, -1.5, 0), (1.2, 1.5, 0), (1.2, 1.5, 1.2), (1.2, -1.5, 1.2)], WALL)
        self._polygon(draw, camera, [(-0.4, -1.5, 0), (1.2, -1.5, 0), (1.2, 1.5, 0), (-0.4, 1.5, 0)], TABLE)
        for i in range(-8, 25):             # a 5 cm grid on the table, so motion is visible
            x = i * 0.05
            self._line(draw, camera, (x, -1.5, 0.0005), (x, 1.5, 0.0005), (158, 137, 108))
        for i in range(-30, 31):
            self._line(draw, camera, (-0.4, i * 0.05, 0.0005), (1.2, i * 0.05, 0.0005), (158, 137, 108))
        order = sorted(self.objects, key=lambda o: -self._depth(camera, o))
        for obj in order:
            getattr(self, f"_draw_{obj['kind']}")(image, draw, camera, obj)
        self._draw_fingers(draw, frames["link6"], camera, gripper)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality)
        return out.getvalue()

    # ---- drawing helpers ----

    def _depth(self, camera, obj) -> float:
        return self.project(camera, self.center(obj))[2]

    def _clip(self, camera, points):
        """Camera-space polygon clipped to the near plane (Sutherland-Hodgman), as image points."""
        local = [apply(transpose(camera.r), [a - b for a, b in zip(p, camera.t)]) for p in points]
        out = []
        for i, a in enumerate(local):
            b = local[(i + 1) % len(local)]
            a_in, b_in = a[2] > NEAR, b[2] > NEAR
            if a_in:
                out.append(a)
            if a_in != b_in:
                t = (NEAR - a[2]) / (b[2] - a[2])
                out.append([a[k] + t * (b[k] - a[k]) for k in range(3)])
        return [(FOCAL * p[0] / p[2] + WIDTH / 2, FOCAL * p[1] / p[2] + HEIGHT / 2) for p in out]

    def _polygon(self, draw, camera, points, color, outline=None):
        pts = self._clip(camera, points)
        if len(pts) >= 3:
            draw.polygon(pts, fill=tuple(int(c) for c in color), outline=outline)

    def _line(self, draw, camera, a, b, color, width=1):
        pts = self._clip(camera, [a, b, b])
        if len(pts) >= 2:
            draw.line(pts[:2], fill=color, width=width)

    def _box(self, draw, camera, obj, color, top_color=None):
        sx, sy, sz = obj["size"]
        x, y, z = obj["xyz"]
        xs, ys, zs = (x - sx / 2, x + sx / 2), (y - sy / 2, y + sy / 2), (z, z + sz)
        faces = [  # (corners, outward normal, shade)
            ([(xs[0], ys[0], zs[1]), (xs[1], ys[0], zs[1]), (xs[1], ys[1], zs[1]), (xs[0], ys[1], zs[1])], (0, 0, 1), 1.0),
            ([(xs[0], ys[0], zs[0]), (xs[0], ys[1], zs[0]), (xs[0], ys[1], zs[1]), (xs[0], ys[0], zs[1])], (-1, 0, 0), 0.75),
            ([(xs[1], ys[0], zs[0]), (xs[1], ys[0], zs[1]), (xs[1], ys[1], zs[1]), (xs[1], ys[1], zs[0])], (1, 0, 0), 0.6),
            ([(xs[0], ys[0], zs[0]), (xs[0], ys[0], zs[1]), (xs[1], ys[0], zs[1]), (xs[1], ys[0], zs[0])], (0, -1, 0), 0.7),
            ([(xs[0], ys[1], zs[0]), (xs[1], ys[1], zs[0]), (xs[1], ys[1], zs[1]), (xs[0], ys[1], zs[1])], (0, 1, 0), 0.85),
        ]
        for corners, normal, shade in faces:
            centre = [sum(c[k] for c in corners) / 4 for k in range(3)]
            toward = [camera.t[k] - centre[k] for k in range(3)]
            if sum(n * t for n, t in zip(normal, toward)) <= 0:
                continue
            base = top_color if (normal == (0, 0, 1) and top_color) else color
            self._polygon(draw, camera, corners, [c * shade for c in base])

    def _textured(self, image, camera, corners, texture):
        """Paste `texture` onto a 3D quad (corners in texture order: top-left, top-right, bottom-right,
        bottom-left) with a perspective warp. Skipped if any corner is behind the camera."""
        from PIL import Image

        projected = [self.project(camera, c) for c in corners]
        if any(z <= NEAR or math.isnan(u) for u, v, z in projected):
            return
        dst = [(u, v) for u, v, _ in projected]
        if max(abs(u) for u, _ in dst) > 4 * WIDTH or max(abs(v) for _, v in dst) > 4 * HEIGHT:
            return
        w, h = texture.size
        coeffs = homography(dst, [(0, 0), (w, 0), (w, h), (0, h)])
        if coeffs is None:
            return
        rgba = texture.convert("RGBA")
        warped = rgba.transform((WIDTH, HEIGHT), Image.PERSPECTIVE, coeffs, Image.BILINEAR)
        image.paste(warped, (0, 0), warped)

    def _draw_block(self, image, draw, camera, obj):
        self._box(draw, camera, obj, COLORS.get(obj.get("color"), (200, 40, 40)))

    def _draw_board(self, image, draw, camera, obj):
        self._box(draw, camera, obj, COLORS.get(obj.get("color"), COLORS["pcb"]))

    def _draw_button(self, image, draw, camera, obj):
        pressed = obj.get("_down")
        shown = dict(obj, size=[obj["size"][0], obj["size"][1], obj["size"][2] * (0.4 if pressed else 1.0)])
        self._box(draw, camera, shown, COLORS["grey"], top_color=COLORS.get(obj.get("color"), COLORS["black"]))

    def _draw_led(self, image, draw, camera, obj):
        x, y, z = obj["xyz"]
        on = obj.get("on", False)
        color = COLORS.get(obj.get("color"), COLORS["green"])
        lit = color if on else tuple(int(c * 0.25) + 20 for c in color)
        if on:      # a glow around a lit LED
            self._polygon(draw, camera, circle((x, y, z + 0.001), 0.006), [min(255, c * 0.6 + 100) for c in color])
        self._box(draw, camera, {"xyz": [x, y, z], "size": [0.005, 0.003, 0.002]}, lit, top_color=lit)

    def _flat_texture(self, image, camera, obj, texture, z_lift=0.0005):
        sx, sy = obj["size"][:2]
        x, y, z = obj["xyz"]
        z += z_lift
        # texture top = far edge (+x), left = +y, as read by someone at the arm looking forward
        corners = [(x + sx / 2, y + sy / 2, z), (x + sx / 2, y - sy / 2, z),
                   (x - sx / 2, y - sy / 2, z), (x - sx / 2, y + sy / 2, z)]
        self._textured(image, camera, corners, texture)

    def _draw_display(self, image, draw, camera, obj):
        self._flat_texture(image, camera, obj, text_texture(str(obj.get("text", "")), (20, 20, 20), (255, 60, 40),
                                                            obj["size"], seven_segment=True))

    def _draw_note(self, image, draw, camera, obj):
        self._flat_texture(image, camera, obj, text_texture(str(obj.get("text", "")), (250, 245, 200), (20, 20, 20),
                                                            obj["size"]))

    def _draw_mark(self, image, draw, camera, obj):
        x, y, z = obj["xyz"]
        color = COLORS.get(obj.get("color"), COLORS["yellow"])
        for (dx, dy) in ((0.025, 0.004), (0.004, 0.025)):
            self._polygon(draw, camera, [(x - dx, y - dy, 0.0008), (x + dx, y - dy, 0.0008),
                                         (x + dx, y + dy, 0.0008), (x - dx, y + dy, 0.0008)], color)

    def _draw_hand(self, image, draw, camera, obj):
        w, h = obj.get("size", [0.09, 0.18])
        x, y, z = obj["xyz"]
        # upright, facing the arm: a vertical quad across y at distance x
        corners = [(x, y + w / 2, z + h), (x, y - w / 2, z + h), (x, y - w / 2, z), (x, y + w / 2, z)]
        self._textured(image, camera, corners, hand_texture())

    def _draw_fingers(self, draw, link6: Pose, camera, gripper):
        inward = FINGER_TRAVEL * (1.0 - min(max(gripper, 0.0), 1.0))
        for side in (1, -1):
            y0 = side * (FINGER_BASE[1] - inward)
            for dz, shade in ((0.006, 1.0), (-0.006, 0.8)):
                pts = [link6.point((FINGER_BASE[0], y0, dz)), link6.point((FINGER_BASE[0] + FINGER_LENGTH, y0, dz)),
                       link6.point((FINGER_BASE[0] + FINGER_LENGTH, y0 - side * 0.006, dz)),
                       link6.point((FINGER_BASE[0], y0 - side * 0.006, dz))]
                self._polygon(draw, camera, pts, [70 * shade, 70 * shade, 75 * shade])


# ---------------- textures and geometry helpers ----------------

def circle(center, radius, n=16):
    x, y, z = center
    return [(x + radius * math.cos(2 * math.pi * i / n), y + radius * math.sin(2 * math.pi * i / n), z)
            for i in range(n)]


def homography(dst, src):
    """Coefficients for PIL's PERSPECTIVE transform mapping output points `dst` to texture points `src`."""
    rows, rhs = [], []
    for (x, y), (u, v) in zip(dst, src):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rhs.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        rhs.append(v)
    return solve(rows, rhs)


def solve(a, b):
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] for i in range(n)]


_TEXTURES: dict = {}


def text_texture(text, background, ink, size, seven_segment=False):
    from PIL import Image, ImageDraw, ImageFont

    key = (text, background, ink, tuple(size), seven_segment)
    if key in _TEXTURES:
        return _TEXTURES[key]
    w = 400
    h = max(40, int(w * size[1] / size[0]))
    texture = Image.new("RGB", (w, h), background)
    draw = ImageDraw.Draw(texture)
    lines = wrap(text, 22 if not seven_segment else 8)
    font_size = int(min(h / max(1, len(lines)) * 0.7, w / max(1, max((len(line) for line in lines), default=1)) * 1.6))
    try:
        font = ImageFont.load_default(size=max(10, font_size))
    except TypeError:       # Pillow < 10.1
        font = ImageFont.load_default()
    y = (h - font_size * len(lines) * 1.15) / 2
    for line in lines:
        width = draw.textlength(line, font=font)
        draw.text(((w - width) / 2, y), line, fill=ink, font=font)
        y += font_size * 1.15
    _TEXTURES[key] = texture
    return texture


def wrap(text, width):
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width and line:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + ([line] if line else []) or [""]


def hand_texture():
    """A flat drawing of an open hand, palm towards the camera, on a transparent background."""
    from PIL import Image, ImageDraw

    if "hand" in _TEXTURES:
        return _TEXTURES["hand"]
    w, h = 200, 400
    texture = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(texture)
    skin, edge = (224, 172, 138, 255), (170, 120, 95, 255)
    draw.rectangle([55, 260, 145, 400], fill=skin, outline=edge, width=3)          # wrist / forearm
    draw.rounded_rectangle([30, 160, 170, 300], radius=40, fill=skin, outline=edge, width=3)   # palm
    for i, (x, top) in enumerate([(38, 40), (72, 15), (106, 22), (140, 55)]):        # fingers
        draw.rounded_rectangle([x - 2, top, x + 28, 200], radius=14, fill=skin, outline=edge, width=3)
    draw.rounded_rectangle([0, 190, 70, 230], radius=18, fill=skin, outline=edge, width=3)  # thumb
    _TEXTURES["hand"] = texture
    return texture


def load_scenes(path: str):
    """Add the named scenes from a YAML file ({name: [objects]}) to the presets."""
    with open(path, encoding="utf-8") as f:
        SCENES.update(yaml.safe_load(f) or {})
