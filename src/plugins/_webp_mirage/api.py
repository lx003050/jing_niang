"""Python image I/O + sizing; C++ implements the four-frame RIFF container."""
from __future__ import annotations
import ctypes as ct
from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path
import re
import sys
import tempfile
import warnings
from PIL import Image, ImageChops, ImageCms, ImageColor, ImageOps, features

SAMPLE_MINIMUM_BYTES = 2359296
MAX_PIXELS = 40_000_000
MAX_FILE_BYTES = 512 * 1024 * 1024


def _integer(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}..{high}')
    return value


@lru_cache(maxsize=1)
def _native():
    suffix = '.dll' if sys.platform == 'win32' else '.dylib' if sys.platform == 'darwin' else '.so'
    path = Path(__file__).with_name('mirage' + suffix)
    if not path.is_file():
        raise RuntimeError(f'Native library missing: {path}. Run python build.py first.')
    lib = ct.CDLL(str(path))
    byte_ptr = ct.POINTER(ct.c_uint8)
    lib.mirage_abi_version.restype = ct.c_uint32
    if lib.mirage_abi_version() != 1:
        raise RuntimeError('Native library ABI mismatch; rebuild it.')
    lib.mirage_pack.argtypes = [ct.POINTER(byte_ptr), ct.POINTER(ct.c_size_t),
        ct.c_uint32, ct.c_uint32, ct.c_uint32, ct.c_uint64, ct.c_char_p,
        ct.POINTER(byte_ptr), ct.POINTER(ct.c_size_t), ct.POINTER(ct.c_char), ct.c_size_t]
    lib.mirage_pack.restype = ct.c_int
    lib.mirage_free.argtypes = [ct.c_void_p]
    lib.mirage_free.restype = None
    return lib


def pack_frames(frames, *, fast_ms=1, hold_ms=600000, loops=1,
                minimum_bytes=SAMPLE_MINIMUM_BYTES, source_extension='.png') -> bytes:
    """Low-level C ABI wrapper. Supply four lossless static WebP byte strings."""
    _integer('fast_ms', fast_ms, 1, 0xffffff)
    _integer('hold_ms', hold_ms, 1, 0xffffff)
    _integer('loops', loops, 0, 65535)
    _integer('minimum_bytes', minimum_bytes, 0, MAX_FILE_BYTES)
    if not isinstance(source_extension, str) or not re.fullmatch(r'[.a-z0-9]{0,16}', source_extension):
        raise ValueError('Invalid source_extension')
    if len(frames) != 4 or any(not isinstance(f, bytes) for f in frames):
        raise ValueError('Exactly four bytes objects are required')
    if sum(map(len, frames)) > MAX_FILE_BYTES - 4096:
        raise ValueError('Encoded inputs exceed memory limit')
    lib = _native()
    byte_ptr = ct.POINTER(ct.c_uint8)
    buffers = [(ct.c_uint8 * len(f)).from_buffer_copy(f) for f in frames]
    pointers = (byte_ptr * 4)(*[ct.cast(b, byte_ptr) for b in buffers])
    sizes = (ct.c_size_t * 4)(*[len(f) for f in frames])
    output, length = byte_ptr(), ct.c_size_t()
    error = ct.create_string_buffer(1024)
    status = lib.mirage_pack(pointers, sizes, fast_ms, hold_ms, loops, minimum_bytes,
        source_extension.encode('ascii'), ct.byref(output), ct.byref(length), error, len(error))
    if status:
        raise ValueError('Native packer: ' + error.value.decode('utf-8', errors='replace'))
    try:
        return ct.string_at(output, length.value)
    finally:
        lib.mirage_free(output)


def _load(path, frame_index):
    _integer('frame_index', frame_index, 0, 2**31 - 1)
    with Image.open(path) as original:
        if original.width * original.height > MAX_PIXELS:
            raise ValueError(f'Input exceeds {MAX_PIXELS:,} pixels: {path}')
        count = getattr(original, 'n_frames', 1)
        if frame_index >= count:
            raise ValueError(f'Frame {frame_index} unavailable ({count} frames): {path}')
        original.seek(frame_index)
        image = ImageOps.exif_transpose(original).copy()
        profile = original.info.get('icc_profile')
    if image.width * image.height > MAX_PIXELS:
        raise ValueError('Selected frame exceeds pixel limit')
    if profile:
        try:
            alpha = image.convert('RGBA').getchannel('A')
            base = image if image.mode in ('RGB', 'CMYK', 'LAB', 'L') else image.convert('RGB')
            image = ImageCms.profileToProfile(base, ImageCms.ImageCmsProfile(BytesIO(profile)),
                ImageCms.createProfile('sRGB'), outputMode='RGB').convert('RGBA')
            image.putalpha(alpha)
        except (OSError, ValueError, ImageCms.PyCMSError) as exc:
            warnings.warn(f'Cannot convert ICC profile; using decoded colors: {exc}', stacklevel=2)
    return image.convert('RGBA')


def _background(color):
    if isinstance(color, str):
        color = ImageColor.getrgb(color)
    if not isinstance(color, (tuple, list)) or len(color) != 3:
        raise ValueError('background must be an opaque RGB color, e.g. #ffffff')
    return tuple(_integer('RGB component', c, 0, 255) for c in color)


def _fit(image, size, mode, background):
    base = Image.new('RGBA', image.size, background + (255,))
    image = Image.alpha_composite(base, image).convert('RGB')
    if mode == 'cover':
        return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS).convert('RGBA')
    if mode == 'stretch':
        return image.resize(size, Image.Resampling.LANCZOS).convert('RGBA')
    ratio = min(size[0] / image.width, size[1] / image.height)
    fitted = (max(1, min(size[0], round(image.width * ratio))),
              max(1, min(size[1], round(image.height * ratio))))
    resized = image.resize(fitted, Image.Resampling.LANCZOS)
    canvas = Image.new('RGB', size, background)
    canvas.paste(resized, ((size[0] - resized.width) // 2, (size[1] - resized.height) // 2))
    return canvas.convert('RGBA')


def _patch(image):
    w, h = image.size
    x, y = (w - 1) & ~1, (h - 1) & ~1
    patch = image.crop((x, y, w, h))
    pixel = patch.getpixel((patch.width - 1, patch.height - 1))
    patch.putpixel((patch.width - 1, patch.height - 1), pixel[:3] + (254,))
    return patch


def _encode(image):
    out = BytesIO()
    image.save(out, format='WEBP', lossless=True, quality=100, method=4, exact=True)
    return out.getvalue()


def create(cover, inner, output, *, size=None, fit='contain', background='#ffffff',
           max_side=2048, fast_ms=1, hold_ms=600000, loops=1,
           minimum_bytes=SAMPLE_MINIMUM_BYTES, cover_frame=0, inner_frame=0,
           overwrite=False) -> Path:
    """Create a sample-style color animated WebP.

    size: (width,height), otherwise cover's oriented size capped by max_side.
    fit: contain (letterbox), cover (center crop), stretch (may distort).
    Input animations/multipage files use the selected frame, default first.
    Transparent inputs are flattened against background to prevent bleed-through.
    Default timing: [1,1,600000,600000] ms, loop=1, min size=2.25 MiB.
    """
    if not features.check('webp'):
        raise RuntimeError('This Pillow build lacks WebP support')
    if fit not in ('contain', 'cover', 'stretch'):
        raise ValueError('fit must be contain, cover, or stretch')
    _integer('max_side', max_side, 1, 16383)
    for name, value, low, high in [('fast_ms', fast_ms, 1, 0xffffff),
            ('hold_ms', hold_ms, 1, 0xffffff), ('loops', loops, 0, 65535),
            ('minimum_bytes', minimum_bytes, 0, MAX_FILE_BYTES)]:
        _integer(name, value, low, high)
    background = _background(background)
    cover_path, inner_path, destination = Path(cover), Path(inner), Path(output)
    if destination.suffix.lower() != '.webp':
        raise ValueError('Output filename must end in .webp')
    if destination.resolve() in (cover_path.resolve(), inner_path.resolve()):
        raise ValueError('Output must not replace an input')
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)
    _native()  # fail before expensive preprocessing if not built
    a, b = _load(cover_path, cover_frame), _load(inner_path, inner_frame)
    if size is None:
        ratio = min(1, max_side / max(a.size))
        size = tuple(max(1, round(d * ratio)) for d in a.size)
    else:
        if not isinstance(size, (tuple, list)) or len(size) != 2:
            raise ValueError('size must be (width, height)')
        size = tuple(_integer('dimension', d, 1, 16383) for d in size)
    if size[0] * size[1] > MAX_PIXELS:
        raise ValueError('Output exceeds pixel limit')
    # The container stores a 2x2 corner patch, so both dimensions must be even:
    # an odd side collapses the patch (e.g. 2x1), which breaks QQ playback.
    size = tuple(max(2, d & ~1) for d in size)
    a, b = _fit(a, size, fit, background), _fit(b, size, fit, background)
    # Blend inner over cover. Unchanged RGB pixels can be transparent deltas.
    difference = ImageChops.difference(a.convert('RGB'), b.convert('RGB'))
    r, g, blue = difference.split()
    changed = ImageChops.lighter(ImageChops.lighter(r, g), blue).point(lambda v: 255 if v else 0)
    # The preceding patch makes this pixel slightly transparent: replace it fully.
    changed.putpixel((size[0] - 1, size[1] - 1), 255)
    delta = b.copy()
    delta.putalpha(changed)
    frames = [_encode(a), _encode(_patch(a)), _encode(delta), _encode(_patch(b))]
    extension = inner_path.suffix.lower()
    if not re.fullmatch(r'[.a-z0-9]{0,16}', extension):
        extension = ''
    data = pack_frames(frames, fast_ms=fast_ms, hold_ms=hold_ms, loops=loops,
        minimum_bytes=minimum_bytes, source_extension=extension)
    # Decode all frames before publishing; catches codec/container integration errors.
    with Image.open(BytesIO(data)) as check:
        if check.n_frames != 4 or check.size != size:
            raise RuntimeError('Generated animation failed structural verification')
        for index, duration in enumerate((fast_ms, fast_ms, hold_ms, hold_ms)):
            check.seek(index)
            check.load()
            if check.info.get('duration') != duration:
                raise RuntimeError('Generated animation failed timing verification')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.mirage-', suffix='.tmp', dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        if overwrite:
            os.replace(temporary, destination)
        else:
            # Atomic no-clobber publication; requires a filesystem with hard links.
            os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.resolve()
