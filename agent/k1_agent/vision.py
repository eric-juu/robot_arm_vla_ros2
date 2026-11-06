"""
What a program can measure in a camera frame. A program can't see images, so these are its eyes, the way ASPIRE's
programs get fixed perception functions. They read the JPEG's pixels and nothing else, so they work the same on the
simulator's frames and the real camera's, and can't leak what the simulator knows.
"""
import io

COLORS = {
    "red": lambda r, g, b: r > g + 70 and r > b + 70,
    "yellow": lambda r, g, b: r > 180 and g > 150 and b < 110,
    "green": lambda r, g, b: g > r + 40 and g > b + 25,
    "blue": lambda r, g, b: b > r + 70 and b > g + 50,
    "black": lambda r, g, b: max(r, g, b) < 60,
    "white": lambda r, g, b: min(r, g, b) > 200,
}
MAX_REGION_PIXELS = 20000


def _open(jpeg: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(jpeg))


def find_color(jpeg: bytes, color: str, min_pixels: int = 20) -> list[dict]:
    """The connected blobs of one colour with at least `min_pixels` pixels, largest first."""
    img = _open(jpeg).convert("RGB")
    width = img.width
    data = img.tobytes()
    match = COLORS[color]
    left = {i for i, rgb in enumerate(zip(data[0::3], data[1::3], data[2::3])) if match(*rgb)}
    blobs = []
    while left:
        stack = [left.pop()]
        members = []
        while stack:
            i = stack.pop()
            members.append(i)
            x = i % width
            for n in (i - width, i + width, i - 1 if x else -1, i + 1 if x < width - 1 else -1):
                if n in left:
                    left.remove(n)
                    stack.append(n)
        if len(members) >= min_pixels:
            xs = [i % width for i in members]
            ys = [i // width for i in members]
            blobs.append({"x": round(sum(xs) / len(xs), 1), "y": round(sum(ys) / len(ys), 1), "left": min(xs),
                          "right": max(xs), "top": min(ys), "bottom": max(ys), "pixels": len(members)})
    return sorted(blobs, key=lambda b: -b["pixels"])


def gray_region(jpeg: bytes, left: int, top: int, right: int, bottom: int) -> dict:
    """Brightness of a box, clipped to the frame: width, height and the pixels row by row."""
    img = _open(jpeg).convert("L")
    box = (max(left, 0), max(top, 0), min(right, img.width), min(bottom, img.height))
    width, height = box[2] - box[0], box[3] - box[1]
    if width <= 0 or height <= 0:
        raise ValueError(f"the box is outside the {img.width}x{img.height} frame")
    if width * height > MAX_REGION_PIXELS:
        raise ValueError(f"the box has {width * height} pixels, the most is {MAX_REGION_PIXELS}")
    return {"width": width, "height": height, "pixels": list(img.crop(box).tobytes())}
