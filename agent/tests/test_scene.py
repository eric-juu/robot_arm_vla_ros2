import math
import os
import xml.etree.ElementTree as ET

import pytest

pytest.importorskip("PIL")

from k1_agent import scene as sc  # noqa: E402
from k1_agent.client import K1Client  # noqa: E402
from k1_agent.primitives import Primitives  # noqa: E402
from k1_agent.sim import SimServer  # noqa: E402

URDF = os.path.join(os.path.dirname(__file__), "..", "..", "ros2", "k1_description", "urdf", "k1_standalone.urdf")
OVERVIEW = [0.0, 0.2, -0.4, 0.0, -1.3, 0.0]
ABOVE = [0.0, -0.55, -0.85, 0.0, 1.2, 0.0]      # gripper level, 4-5 cm over a block at (0.30, 0)
AT_BLOCK = [0.0, -0.85, -0.75, 0.0, 1.35, 0.0]  # the block between the fingers


def test_kinematics_match_the_urdf():
    joints = {j.get("name"): j for j in ET.parse(URDF).getroot().iter("joint")}
    for i, (xyz, axis) in enumerate(sc.CHAIN, start=1):
        j = joints[f"joint{i}"]
        assert [float(v) for v in j.find("origin").get("xyz").split()] == pytest.approx(list(xyz))
        assert [float(v) for v in j.find("axis").get("xyz").split()] == pytest.approx(list(axis))
        assert [float(v) for v in j.find("origin").get("rpy").split()] == [0.0, 0.0, 0.0]
    cam = joints["joint_usb_cam_frame"].find("origin")
    assert [float(v) for v in cam.get("xyz").split()] == pytest.approx(list(sc.CAMERA_OFFSET))
    assert [float(v) for v in cam.get("rpy").split()] == pytest.approx(list(sc.CAMERA_RPY))
    grasp = joints["joint_grasping_frame"].find("origin")
    assert [float(v) for v in grasp.get("xyz").split()] == pytest.approx(list(sc.GRASP_OFFSET))


def test_forward_kinematics_and_camera():
    frames = sc.forward([0.0] * 6)
    assert frames["grasp"].t == pytest.approx([0.364, 0.0, 0.304], abs=0.002)
    forward_axis = [frames["camera"].r[i][2] for i in range(3)]
    assert forward_axis == pytest.approx([1.0, 0.0, 0.0], abs=0.01)        # looking straight ahead at zero
    looking_down = [sc.forward(OVERVIEW)["camera"].r[i][2] for i in range(3)]
    assert looking_down[2] < -0.95
    turned = sc.forward([0.5, 0, 0, 0, 0, 0])["grasp"].t
    assert turned[1] < 0        # positive shoulder_pan turns toward -y (the axis is -z)


def test_render_and_in_view():
    scene = sc.Scene.from_spec({"base": "tabletop", "objects": [{"name": "blue_block", "xyz": [0.31, -0.07, 0.0]}]})
    jpeg = scene.render(OVERVIEW)
    assert jpeg.startswith(b"\xff\xd8") and len(jpeg) > 5000
    red, blue = scene.in_view("red_block", OVERVIEW), scene.in_view("blue_block", OVERVIEW)
    assert red["region"] == "centre" and blue["visible"] and blue["u"] > red["u"] + 100   # -y is image right
    assert not scene.in_view("red_block", [0.0] * 6)["visible"]                 # under the camera at zero
    bench = sc.Scene.from_spec("bench")
    assert bench.render(OVERVIEW) != jpeg and bench.in_view("reset", OVERVIEW)["visible"]


def test_contact_press_and_grasp():
    assert sc.Scene([]).collides([0.0, -1.0, -1.4, 0.0, 1.6, 0.0], 1.0)       # into the table
    assert not sc.Scene([]).collides([0.0] * 6, 1.0)

    world = sc.Scene.from_spec([{"name": "block", "kind": "block", "xyz": [0.30, 0.0, 0.0],
                                 "size": [0.03, 0.03, 0.03]}])
    assert not world.collides(ABOVE, 1.0) and not world.collides(AT_BLOCK, 1.0)
    world.step(AT_BLOCK, 0.0)
    assert world.held and world.held["name"] == "block" and world.gripper_stop(0.0) > 0.3
    lifted = [0.0, -0.65, -0.85, 0.0, 1.35, 0.0]
    world.step(lifted, 0.0)
    assert world.get("block")["xyz"][2] > 0.03
    world.step(lifted, 1.0)
    assert world.held is None and world.get("block")["xyz"][2] == 0.0


def test_sim_scene_physics_and_faults():
    blocks = [{"name": "block", "kind": "block", "xyz": [0.30, 0.0, 0.0], "size": [0.03, 0.03, 0.03]}]
    with SimServer(scene=blocks, max_velocity=2.0) as sim:
        with K1Client(sim.url, sim.token) as k1:
            sim.reset(ABOVE)
            k1.wait_for(lambda s: s.get("ready"), 3, after_seq=k1.state_seq)
            prims = Primitives(k1, allow_motion=True, max_step_rad=0.5)
            first = prims.observe()["image"]
            assert prims.move_joints(AT_BLOCK)["ok"]
            assert prims.observe()["image"] != first             # rendered from the new pose
            closed = prims.set_gripper(0.0)
            assert closed["gripper_measured"] > -0.03             # the fingers stop on the block
            assert prims.move_joints({"shoulder_lift": -0.65})["ok"]
            assert sim.scene.get("block")["xyz"][2] > 0.03 and sim.scene.held

            sim.drop_held()
            assert sim.scene.held is None and sim.scene.get("block")["xyz"][2] == 0.0

            # pushing into the table: the arm stops short and the move isn't reached
            sim.reset(AT_BLOCK, scene=[])
            k1.wait_for(lambda s: s.get("ready"), 3, after_seq=k1.state_seq)
            result = prims.move_joints({"shoulder_lift": -1.0, "elbow_flex": -1.4})
            assert result["error"] == "not_reached" and sim.contacts > 0

            sim.reset([0.0] * 6, scene=[])
            k1.wait_for(lambda s: s.get("ready"), 3, after_seq=k1.state_seq)
            sim.set_offset("elbow_flex", 0.1)
            assert prims.move_joints({"elbow_flex": 0.2})["error"] == "not_reached"
            sim.camera(False)
            assert prims.observe().get("image") is None
            sim.camera(True)
            sim.go_stale(1.5)
            k1.wait_for(lambda s: s.get("stale"), 3)
            assert prims.move_joints({"shoulder_pan": 0.1})["error"] in ("stale_state", "not_ready")


def test_button_counts_presses():
    world = sc.Scene.from_spec("bench")
    pressing = [0.1, -0.7, 0.25, 0.0, -1.4, 0.0]        # gripper closed and pointing down onto the button
    assert not world.collides(pressing, 0.0)
    assert world.get("display")["text"] == "42" and not world.get("stat")["on"]
    world.step(pressing, 0.0)
    assert world.get("reset")["presses"] == 1
    assert world.get("display")["text"] == "43" and world.get("stat")["on"]     # the board rebooted
    world.step(pressing, 0.0)                                # still held down: not a second press
    assert world.get("reset")["presses"] == 1
    world.step([0.0] * 6, 0.0)
    world.step(pressing, 0.0)
    assert world.get("reset")["presses"] == 2
    assert math.isfinite(world.in_view("reset", OVERVIEW)["distance"])
