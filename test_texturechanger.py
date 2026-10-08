"""Run: python test_texturechanger.py"""
import io

from PIL import Image
from srctools.vtf import VTF, ImageFormats, VTFFlags

from texturechanger import base_textures, get_pin, make_vtf, set_pin


def orig(fmt, flags=VTFFlags.EMPTY):
    v = VTF(256, 128, (7, 2), fmt=fmt, flags=flags)
    v.version = (7, 1)  # many CS:S textures are 7.1, which srctools can't write
    return v


def roundtrip(img, o):
    return VTF.read(io.BytesIO(make_vtf(img, o)))


opaque = Image.new('RGB', (1000, 700), (10, 200, 30))
seethrough = Image.new('RGBA', (64, 64), (255, 0, 0, 100))

r = roundtrip(opaque, orig(ImageFormats.DXT1, VTFFlags.CLAMP_S))
assert (r.width, r.height, r.format, r.version) == (256, 128, ImageFormats.DXT1, (7, 2))
assert r.flags & VTFFlags.CLAMP_S
assert abs(r.get().to_PIL().getpixel((5, 5))[1] - 200) < 8

r = roundtrip(seethrough, orig(ImageFormats.DXT1))  # transparency upgrades DXT1 -> DXT5
assert r.format == ImageFormats.DXT5 and abs(r.get().to_PIL().getpixel((5, 5))[3] - 100) < 8

assert roundtrip(opaque, orig(ImageFormats.I8)).format == ImageFormats.DXT5  # unusual format -> DXT5

# In-game size: scale multiplies the original's pixel size, capped at 4096; crop keeps the shape the same.
assert (roundtrip(opaque, orig(ImageFormats.DXT1)).width) == 256
r = VTF.read(io.BytesIO(make_vtf(opaque, orig(ImageFormats.DXT1), scale=2)))
assert (r.width, r.height) == (512, 256)
r = VTF.read(io.BytesIO(make_vtf(opaque, orig(ImageFormats.DXT1), scale=32, crop=True)))
assert (r.width, r.height) == (4096, 2048)

try:
    make_vtf(opaque, orig(ImageFormats.DXT1, VTFFlags.ENVMAP))
    raise AssertionError('cubemap should be rejected')
except ValueError:
    pass

# Material resolving: patch include, $basetexture2, quoted path with spaces, backslashes.
vmts = {
    'materials/maps/m/brick_1_2_3.vmt': b'"patch" { "include" "materials/Brick\\Wall.vmt" }',
    'materials/brick/wall.vmt': b'"LightmappedGeneric" { "$basetexture" "Brick/Wall A"\n $basetexture2 brick\\moss }',
}
find = lambda p: ('map', lambda: vmts[p]) if p in vmts else None
assert base_textures('MAPS/M/BRICK_1_2_3', find) == ['materials/brick/wall a.vtf', 'materials/brick/moss.vtf']
assert base_textures('missing/mat', find) == []

# Pinning a map in gameinfo.txt: goes right below custom/*, re-pin replaces, unpin restores the exact bytes.
gi = 'SearchPaths\r\n{\r\n\t\t\tgame+mod\t\t\tcstrike/custom/*\r\n\t\t\tgame+mod\t\t\tcstrike/cstrike_pak.vpk\r\n}\r\n'
one = set_pin(gi, 'download/maps/bhop_a.bsp')
assert one.splitlines()[3] == '\t\t\tgame\t\t\t\t"|gameinfo_path|download/maps/bhop_a.bsp"\t// texturechanger pin'
assert '\r\n' in one and '\n' not in one.replace('\r\n', '')  # kept CRLF line endings
two = set_pin(one, 'maps/bhop b.bsp')
assert get_pin(two) == 'maps/bhop b.bsp' and two.count('texturechanger pin') == 1
assert set_pin(two, None) == gi and get_pin(gi) is None
try:
    set_pin('no search paths here', 'maps/x.bsp')
    raise AssertionError('missing custom line should raise')
except ValueError:
    pass

print('ok')
