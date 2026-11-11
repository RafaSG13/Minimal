import importlib.util
import tempfile
import unittest
from pathlib import Path

from minimal.extensions.logo import convert, extension_source
from minimal.extensions.registry import Registry


@unittest.skipUnless(importlib.util.find_spec("PIL"), "needs Pillow")
class ConvertTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def image(self, size, pixels):
        from PIL import Image

        img = Image.new("RGBA", size)
        img.putdata(pixels)
        path = self.tmp / "img.png"
        img.save(path)
        return path

    def test_pixel_art_keeps_exact_colours_and_loads_as_extension(self):
        red, blue, clear = (255, 0, 0, 255), (0, 0, 255, 255), (0, 0, 0, 0)
        art, colors = convert(self.image((3, 2), [red, clear, blue, red, red, blue]))
        self.assertEqual(len(art), 2)
        self.assertEqual(art[0][1], ".")
        self.assertEqual(colors[art[0][0]], "#ff0000")
        self.assertEqual(colors[art[0][2]], "#0000ff")
        ns = {}
        exec(extension_source(art, colors, "x"), ns)
        registry = Registry()
        ns["register"](registry)
        self.assertEqual(registry.logo.art, art)

    def test_fully_transparent_image_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "fully transparent"):
            convert(self.image((2, 2), [(0, 0, 0, 0)] * 4))


if __name__ == "__main__":
    unittest.main()
