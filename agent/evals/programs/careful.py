"""
The reference program for the press-diagnosis tasks: one program for all eight, no model, and it decides only from
what the camera shows. It proves the tasks can be solved this way. An agent learning to write its own must never be
shown this file.

    k1-agent eval run evals/diagnosis-tasks.yaml --models program:careful --trials 6
"""
ABOVE = [0.1, -0.55, 0.25, 0.0, -1.4, 0.0]
PRESS = [0.1, -0.7, 0.25, 0.0, -1.4, 0.0]
OVERVIEW = [0.0, 0.2, -0.4, 0.0, -1.3, 0.0]
TAPE_LEFT, TAPE_RIGHT = 142.0, 556.0    # where the crosses sit in a correct 'overview' frame
TAPE_TOLERANCE = 15                     # px; further off than this, the arm has lost its zero
BIG_SHIFT = 25                          # px; a board further off than this needs its press re-aimed
PAN_STEP = 0.05                         # rad, to measure how far the image moves per radian of pan
NUDGES = (0.03, -0.03)                  # rad; a board nudged too little to measure, so test presses either side


def tape(k1):
    """The x of the left and right crosses."""
    blobs = k1.find_color("yellow")["blobs"][:2]
    return sorted(b["x"] for b in blobs)


def board_x(k1):
    blobs = k1.find_color("green")["blobs"]
    return (min(b["left"] for b in blobs) + max(b["right"] for b in blobs)) / 2


def display(k1, board):
    """The display's pixels: it sits 70 px to the left of the board's centre at 'overview'."""
    return k1.gray_region(int(board - 100), 180, int(board - 40), 270)["pixels"]


def changed(before, after):
    return sum(abs(a - b) > 50 for a, b in zip(before, after)) > 40


def press(k1, pan):
    k1.move_joints([pan] + ABOVE[1:])
    k1.set_gripper(0.0)
    k1.move_joints([pan] + PRESS[1:])
    k1.move_joints([pan] + ABOVE[1:])
    k1.go_pose("overview")


def run(k1):
    left, right = tape(k1)
    off = ((left - TAPE_LEFT) + (right - TAPE_RIGHT)) / 2
    if abs(off) > TAPE_TOLERANCE:
        return {"success": False, "summary": f"tape crosses {off:.0f} px off.\ncause: arm out of calibration"}
    if sum(b["pixels"] for b in k1.find_color("blue")["blobs"]) > 80:
        return {"success": False, "summary": "something on the button.\ncause: object on the button"}

    board = board_x(k1)
    before = display(k1, board)
    board_off = board - (left + right) / 2
    big = abs(board_off) > BIG_SHIFT
    pan = ABOVE[0]
    if big:     # the image's response to pan, from the frames, then cancel the board's offset
        k1.move_joints([OVERVIEW[0] + PAN_STEP] + OVERVIEW[1:])
        pan -= board_off / ((board_x(k1) - board) / PAN_STEP)

    for nudge in (0.0,) + NUDGES:
        press(k1, pan + nudge)
        if changed(before, display(k1, board)):
            cause = "board moved" if big or nudge else "none"
            return {"success": True, "summary": f"reset done.\ncause: {cause}"}
    cause = "board moved and unresponsive" if big else "board unresponsive"
    return {"success": False, "summary": f"no change after testing.\ncause: {cause}"}
