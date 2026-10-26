import io

from PIL import Image

from k1_web.image import rotate_jpeg_180


def test_rotate_jpeg_180_turns_the_frame_upside_down():
    image = Image.new("RGB", (64, 48), "black")
    image.paste((255, 255, 255), (0, 0, 16, 12))      # white block in the top left corner
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=95)

    rotated = Image.open(io.BytesIO(rotate_jpeg_180(buf.getvalue())))
    assert rotated.size == (64, 48)
    assert rotated.getpixel((60, 44))[0] > 200          # the block is now bottom right
    assert rotated.getpixel((4, 4))[0] < 50
