"""Camera frame helpers. No ROS imports, so they can be unit tested."""
import io

from PIL import Image


def rotate_jpeg_180(jpeg: bytes, quality: int = 85) -> bytes:
    """Turns a JPEG frame upside down (for a camera mounted upside down)."""
    with Image.open(io.BytesIO(jpeg)) as image:
        out = io.BytesIO()
        image.transpose(Image.Transpose.ROTATE_180).save(out, format="JPEG", quality=quality)
        return out.getvalue()
