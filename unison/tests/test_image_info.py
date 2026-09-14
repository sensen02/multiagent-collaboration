import hashlib
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from unison.image_info import image_info
from unison.runtime import Runtime
from unison.tools import schemas_for


def png(width, height):
    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload
                + struct.pack('>I', zlib.crc32(kind + payload)))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\0' * width * 3) * height))
            + chunk(b'IEND', b''))


def jpeg(marker=0xc0):
    # Metadata-only fixture: APP1 followed by a one-component SOF.
    return (b'\xff\xd8\xff\xe1\x00\x06test\xff' + bytes([marker])
            + struct.pack('>HBHHB', 11, 8, 17, 23, 1) + b'\x01\x11\x00\xff\xd9')


def webp(kind, payload, prefix=b''):
    chunk = kind + struct.pack('<I', len(payload)) + payload + b'\0' * (len(payload) & 1)
    body = b'WEBP' + prefix + chunk
    return b'RIFF' + struct.pack('<I', len(body)) + body


class ImageInfoTests(unittest.TestCase):
    def test_formats_dimensions_size_and_digest(self):
        cases = [
            (png(23, 17), 'PNG'),
            (b'GIF87a' + struct.pack('<HH', 23, 17) + b'\0\0\0', 'GIF'),
            (b'GIF89a' + struct.pack('<HH', 23, 17) + b'\0\0\0', 'GIF'),
            (jpeg(), 'JPEG'), (jpeg(0xc2), 'JPEG'),
            (webp(b'VP8 ', b'\0\0\0\x9d\x01\x2a' + struct.pack('<HH', 23, 17)), 'WebP'),
            (webp(b'VP8L', b'\x2f' + struct.pack('<I', 22 | (16 << 14))), 'WebP'),
            (webp(b'VP8X', b'\x02\0\0\0' + (22).to_bytes(3, 'little')
                  + (16).to_bytes(3, 'little')), 'WebP'),
            (webp(b'VP8L', b'\x2f' + struct.pack('<I', 22 | (16 << 14)),
                  b'JUNK\x01\0\0\0x\0'), 'WebP'),
        ]
        for data, fmt in cases:
            with self.subTest(format=fmt, header=data[:30]):
                self.assertEqual(image_info(data), {'format': fmt, 'width': 23, 'height': 17,
                    'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})

    def test_truncated_and_unsupported_headers(self):
        cases = [b'', b'not an image', b'BM' + b'\0' * 40,
                 png(1, 1)[:32], b'GIF89a', jpeg()[:22], b'\xff\xd8\xff\xd9',
                 b'\xff\xd8\xff\xe1\0\x01', b'\xff\xd8\xff',
                 webp(b'VP8X', b'\0' * 9), webp(b'VP8L', b'\0' * 5),
                 webp(b'VP8 ', b'\0' * 10), webp(b'JUNK', b'abc'),
                 webp(b'VP8X', b'\0' * 10)[:-1], png(0, 1)]
        for data in cases:
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                image_info(data)

    def test_dimensions_are_not_business_constraints(self):
        # An unusual aspect ratio is metadata, not a reason to reject the image.
        result = image_info(png(1, 321))
        self.assertEqual((result['width'], result['height']), (1, 321))

    def test_schema_is_available_to_all_agents(self):
        for groups in [('all',), ('child',)]:
            schema = next(t['function'] for t in schemas_for(groups)
                          if t['function']['name'] == 'workspace_image_info')
            self.assertEqual(schema['parameters']['required'], ['path'])
            self.assertEqual(schema['parameters']['properties'], {'path': {'type': 'string'}})
            self.assertFalse(schema['parameters']['additionalProperties'])


class ImageInfoHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_path_content_detection_and_read_only(self):
        # The handler uses only the existing workspace resolver, not runtime state.
        runtime = Runtime.__new__(Runtime)
        with tempfile.TemporaryDirectory() as tmp:
            task = {'workspace': tmp}
            path = Path(tmp) / 'image.wrong-extension'
            data = png(3, 2)
            path.write_bytes(data)
            before = path.stat().st_mtime_ns
            result = await runtime.tool_workspace_image_info(task, {'path': path.name})
            self.assertEqual(result, {'path': str(path.resolve()), **image_info(data)})
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mtime_ns, before)
            with self.assertRaises(FileNotFoundError):
                await runtime.tool_workspace_image_info(task, {'path': 'missing.png'})
            path.write_text('not an image')
            with self.assertRaises(ValueError):
                await runtime.tool_workspace_image_info(task, {'path': path.name})
