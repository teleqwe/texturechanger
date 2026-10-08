"""CS:S Texture Changer: replace Counter-Strike: Source textures via cstrike/custom/<mod>."""
import base64
import ctypes
import functools
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from functools import partial
from pathlib import Path
from zipfile import BadZipFile

import webview
from PIL import Image, ImageOps
from srctools.bsp import BSP, BSP_LUMPS
from srctools.vpk import VPK
from srctools.vtf import VTF, ImageFormats, VTFFlags

TITLE = 'CS:S Texture Changer'
WINDOWS = sys.platform == 'win32'
SETTINGS = (os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'CSS Texture Changer', 'settings.json')
            if WINDOWS else
            os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')), 'texturechanger', 'settings.json'))
MOD_NAME = 'texturechanger'
GLOBAL = '_global'  # profile for changes made under "All game textures"; always installed
# Same order as gameinfo.txt, so the first VPK containing a file is the one the game uses.
VPKS = ['cstrike/cstrike_pak_dir.vpk', 'hl2/hl2_textures_dir.vpk', 'hl2/hl2_misc_dir.vpk']
KEEP_FORMATS = {ImageFormats.DXT1, ImageFormats.DXT1_ONEBITALPHA, ImageFormats.DXT5,
                ImageFormats.BGR888, ImageFormats.RGB888, ImageFormats.BGRA8888, ImageFormats.RGBA8888}
ALPHA_FORMATS = {ImageFormats.DXT5, ImageFormats.BGRA8888, ImageFormats.RGBA8888}
BASETEX_RE = re.compile(r'"?\$basetexture2?"?\s+(?:"([^"]+)"|(\S+))', re.I)
INCLUDE_RE = re.compile(r'"?include"?\s+"([^"]+)"', re.I)
PREVIEW = 512  # previews use the smallest mipmap at least this wide; the page scales it
THUMB = 128  # texture browser tiles
MAX_SIZE = 4096  # biggest replacement texture side
# source -> (short name for the list, explanation)
SOURCES = {
    'game': ('Game', 'Stock CS:S / HL2 texture. Your replacement shows on every map that uses it.'),
    'cstrike folder': ('Loose', 'Loose file in cstrike/. The custom folder loads first, so your replacement shows.'),
    'download': ('Download', 'Downloaded from a server. The custom folder loads first, so your replacement shows.'),
    'map': ('Map', "Packed inside this map's .bsp file."),
}
PIN_MARK = '// texturechanger pin'
CUSTOM_LINE_RE = re.compile(r'^([ \t]*)game\+mod[ \t]+cstrike/custom/\*[^\n]*\n', re.M | re.I)
PIN_LINE_RE = re.compile(rf'^[^\n]*{re.escape(PIN_MARK)}[^\n]*\n', re.M)
PIN_PATH_RE = re.compile(rf'"\|gameinfo_path\|([^"]+)"[ \t]*{re.escape(PIN_MARK)}')


def steam_dirs():
    """Where Steam is installed: from the registry on Windows, the usual folders on Linux (incl. Flatpak)."""
    if WINDOWS:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
                return [winreg.QueryValueEx(key, 'SteamPath')[0]]
        except OSError:
            return []
    home = os.path.expanduser('~')
    flatpak = os.path.join(home, '.var', 'app', 'com.valvesoftware.Steam')
    return [d for d in (os.path.join(home, '.steam', 'steam'), os.path.join(home, '.local', 'share', 'Steam'),
                        os.path.join(flatpak, '.local', 'share', 'Steam'), os.path.join(flatpak, 'data', 'Steam'))
            if os.path.isdir(d)]


def find_game():
    """Find the CS:S folder through Steam and its library list."""
    libs = []
    for steam in steam_dirs():
        libs.append(steam)
        try:
            with open(os.path.join(steam, 'steamapps', 'libraryfolders.vdf'), encoding='utf-8') as f:
                libs += [p.replace('\\\\', '\\') for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
        except OSError:
            pass
    for lib in libs:
        game = os.path.join(lib, 'steamapps', 'common', 'Counter-Strike Source')
        if os.path.isfile(os.path.join(game, 'cstrike', 'gameinfo.txt')):
            return os.path.normpath(game)
    return None


def load_vpks(game):
    """Map 'materials/....vtf|vmt' -> VPK FileInfo for every material file the game ships."""
    files = {}
    for rel in VPKS:
        path = os.path.join(game, rel)
        if os.path.isfile(path):
            for info in VPK(path):
                if info.ext in ('vtf', 'vmt'):
                    files.setdefault(info.filename.lower(), info)
    return files


def clean(path):
    """Normalize a material/texture reference to 'dir/name' (lower case, no prefix or extension)."""
    path = path.lower().replace('\\', '/').strip('/').removeprefix('materials/')
    return path.removesuffix('.vmt').removesuffix('.vtf')


def base_textures(mat, find, depth=0):
    """'materials/....vtf' paths of a material's $basetexture(s), following patch-material includes."""
    hit = find(f'materials/{clean(mat)}.vmt')
    if not hit or depth > 4:
        return []
    try:
        text = hit[1]().decode('utf-8', 'replace')
    except (BadZipFile, OSError):  # corrupt file packed in a map
        return []
    found = [f'materials/{clean(a or b)}.vtf' for a, b in BASETEX_RE.findall(text)]
    for inc in INCLUDE_RE.findall(text):
        found += base_textures(inc, find, depth + 1)
    return found


def map_textures(bsp_path, lookup):
    """{'materials/....vtf': (source, reader, size)} for the textures a map uses: brushes, skybox, packed files."""
    bsp = BSP(bsp_path)
    pak = {n.lower().replace('\\', '/'): n for n in bsp.pakfile.namelist()}

    def find(path):
        if path in pak:
            return 'map', partial(bsp.pakfile.read, pak[path]), bsp.pakfile.getinfo(pak[path]).file_size
        return lookup(path)

    # ponytail: brushes + skybox + packed files only; static prop textures need MDL parsing.
    mats = [m for m in bsp.textures if not m.lower().startswith('tools/')]
    # Raw regex instead of bsp.ents: srctools rejects some maps' unusual entity outputs.
    sky = re.search(rb'"skyname"\s+"([^"]+)"', bsp.get_lump(BSP_LUMPS.ENTITIES))
    if sky:
        mats += [f'skybox/{sky[1].decode(errors="replace")}{side}' for side in ('rt', 'lf', 'bk', 'ft', 'up', 'dn')]
    out = {}
    for mat in mats:
        for tex in base_textures(mat, find):
            hit = find(tex)
            if hit:
                out[tex] = hit
    for path in pak:  # packed textures that brushes don't reference (models, sprites, ...)
        if path.endswith('.vtf') and not path.startswith('materials/maps/'):  # skip generated cubemaps
            out.setdefault(path, find(path))
    return out


def get_pin(gameinfo):
    """cstrike-relative .bsp path pinned in gameinfo.txt text, or None."""
    m = PIN_PATH_RE.search(gameinfo)
    return m[1] if m else None


def set_pin(gameinfo, bsp):
    """gameinfo.txt text with `bsp` (cstrike-relative path, or None to unpin) mounted right below custom/*.

    A .bsp listed here is mounted at startup, below the custom folders. When that map loads, the engine sees
    it's already mounted and leaves it in place (tested in game), so custom/ beats the map's packed textures.
    """
    gameinfo = PIN_LINE_RE.sub('', gameinfo)
    if bsp:
        m = CUSTOM_LINE_RE.search(gameinfo)
        if not m:
            raise ValueError('Could not find the "cstrike/custom/*" line in gameinfo.txt.')
        nl = '\r\n' if m[0].endswith('\r\n') else '\n'
        line = f'{m[1]}game\t\t\t\t"|gameinfo_path|{bsp}"\t{PIN_MARK}{nl}'
        gameinfo = gameinfo[:m.end()] + line + gameinfo[m.end():]
    return gameinfo


def target_size(orig, scale):
    """Replacement size: the original's size times `scale`, capped at MAX_SIZE.

    The engine maps a texture onto brushes by its pixel size (the map stores texel coordinates, which get
    divided by the texture's width), so scale 2 makes it show twice as big in game: fewer repeats.
    """
    while scale > 1 and max(orig.width, orig.height) * scale > MAX_SIZE:
        scale //= 2
    return orig.width * scale, orig.height * scale


def fit_image(img, size, crop):
    """RGBA image at `size`: stretched, or cropped to keep its proportions."""
    img = img.convert('RGBA')
    return ImageOps.fit(img, size, Image.LANCZOS) if crop else img.resize(size, Image.LANCZOS)


def vtf_format(img, orig):
    """Format and flags for the replacement: the original's, upgraded to DXT5 when the image needs alpha."""
    fmt = orig.format if orig.format in KEEP_FORMATS else ImageFormats.DXT5
    flags = orig.flags
    if fmt not in ALPHA_FORMATS and img.getextrema()[3][0] < 255:  # new image uses transparency
        fmt, flags = ImageFormats.DXT5, (flags & ~VTFFlags.ONEBITALPHA) | VTFFlags.EIGHTBITALPHA
    return fmt, flags


def check_replaceable(orig):
    if orig.flags & VTFFlags.ENVMAP or orig.frame_count > 1:
        raise ValueError('Cubemaps and animated textures can only be replaced with a ready-made .vtf (kept as is).')


def make_vtf(img, orig, scale=1, crop=False):
    """Build VTF bytes from a PIL image with the original texture's format and flags, at `scale` x its size."""
    check_replaceable(orig)
    size = target_size(orig, scale)
    img = fit_image(img, size, crop)
    fmt, flags = vtf_format(img, orig)
    new = VTF(*size, max(orig.version, (7, 2)), fmt=fmt, flags=flags)
    new.get().copy_from(img.tobytes(), ImageFormats.RGBA8888)
    buf = io.BytesIO()
    new.save(buf)
    return buf.getvalue()


def read_vtf(data):
    return VTF.read(io.BytesIO(data))


def preview_image(vtf, size=PREVIEW):
    """Decode the smallest mipmap that is still at least `size` px wide (much faster than full size)."""
    mip = 0
    while (vtf.width >> (mip + 1)) >= size and mip < vtf.mipmap_count - 1:
        mip += 1
    img = vtf.get(mipmap=mip).to_PIL()
    if max(img.size) > size:
        img.thumbnail((size, size))
    return img


def to_data_url(vtf, size=PREVIEW):
    """PNG data URL + size info of a VTF, for the web UI."""
    buf = io.BytesIO()
    preview_image(vtf, size).save(buf, 'PNG')
    return {'url': 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode(),
            'w': vtf.width, 'h': vtf.height, 'fmt': vtf.format.name}


def link_or_copy(src, dst):
    """Hard link (instant, no extra disk space), or a copy where links aren't possible."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def locked(fn):
    """Serialize UI calls (pywebview runs each on its own thread) and turn errors into {'error': ...}."""
    @functools.wraps(fn)
    def wrapper(self, *args):
        with self._lock:
            try:
                return fn(self, *args)
            except Exception as e:
                return {'error': str(e) or type(e).__name__}
    return wrapper


class Api:
    """Everything the web UI calls through window.pywebview.api. Attributes starting with _ stay private."""

    def __init__(self, game):
        self._lock = threading.Lock()
        self._window = None
        self._quitting = False
        self._game = None
        if game:
            self._setup(game)

    def _setup(self, game):
        self._game = game
        self._cstrike = os.path.join(game, 'cstrike')
        self._custom = os.path.join(self._cstrike, 'custom')
        self._mod = os.path.join(self._custom, MOD_NAME)
        self._gameinfo = os.path.join(self._cstrike, 'gameinfo.txt')
        self._pin_file = os.path.join(self._mod, 'pinned_map.txt')  # remembers the pin in case Steam resets gameinfo
        self._profiles = os.path.join(self._mod, '_profiles')  # one folder of replacements per map, plus GLOBAL
        self._vpk = load_vpks(game)
        self._stock = {p: ('game', i.read, i.size) for p, i in self._vpk.items() if p.endswith('.vtf')}
        self._maps = {}
        for folder in ('maps', os.path.join('download', 'maps')):  # cstrike/maps wins, like in game
            for f in Path(self._cstrike, folder).glob('*.bsp'):
                self._maps.setdefault(f.stem.lower(), f)
        self._entries, self._current, self._pending = self._stock, None, {}  # pending: path -> dict, see _stage
        self._thumbs = {}  # (map, path) -> thumbnail; packed textures differ per map
        self._loose = self._index_loose()

    # --- helpers -------------------------------------------------------------------------------------------

    def _lookup(self, path):
        """Where the game finds a non-map file: VPKs, then loose cstrike/, then download/."""
        if path in self._vpk:
            info = self._vpk[path]
            return 'game', info.read, info.size
        hit = self._loose.get(path)
        if hit:
            source, f = hit
            return source, Path(f).read_bytes, os.path.getsize(f)
        return None

    def _index_loose(self):
        """Lower-case 'materials/...' -> (source, file) for loose material files. Lets lookups ignore case like the
        game does, which matters on Linux where file names are case-sensitive."""
        found = {}
        for source, base in (('download', os.path.join(self._cstrike, 'download')), ('cstrike folder', self._cstrike)):
            for d, _, files in os.walk(os.path.join(base, 'materials')):  # cstrike/ comes last, so it wins
                for name in files:
                    full = os.path.join(d, name)
                    found[os.path.relpath(full, base).replace(os.sep, '/').lower()] = (source, full)
        return found

    # Profiles: changes made while a map is loaded belong to that map; changes under "All game textures" are
    # GLOBAL. The folder the game reads (texturechanger/materials) holds GLOBAL + the active map's, see _install.

    def _prof(self):
        return self._current or GLOBAL

    def _pdir(self, profile):
        return os.path.join(self._profiles, profile)

    def _dest(self, path, profile):
        return os.path.join(self._pdir(profile), *path.split('/'))

    def _source_files(self, path, profile):
        """Where the file used for a replacement is kept, so Size/Fit can redo it later at full quality."""
        stem = os.path.join(self._pdir(profile), '_source', *path.split('/'))
        return stem + '.src', stem + '.json'

    def _profile_files(self, profile):
        """Lower-case 'materials/...' paths a profile replaces."""
        base, found = self._pdir(profile), set()
        for dirpath, _, files in os.walk(os.path.join(base, 'materials')):
            rel = os.path.relpath(dirpath, base).replace('\\', '/').lower()
            found.update(f'{rel}/{f.lower()}' for f in files if f.lower().endswith('.vtf'))
        return found

    def _profile_names(self):
        """Maps that have saved changes."""
        try:
            names = os.listdir(self._profiles)
        except OSError:
            return []
        return sorted(n for n in names if n != GLOBAL and self._profile_files(n))

    def _owner(self, path):
        """Profile whose replacement of `path` shows in the current view (the map's own first), or None."""
        for profile in dict.fromkeys((self._prof(), GLOBAL)):
            if os.path.isfile(self._dest(path, profile)):
                return profile
        return None

    def _install(self):
        """Rebuild the folder the game reads from GLOBAL + the active map's profile (the map's wins)."""
        live = os.path.join(self._mod, 'materials')
        shutil.rmtree(live, ignore_errors=True)
        for profile in (GLOBAL, self._pinned()):
            src = profile and os.path.join(self._pdir(profile), 'materials')
            if src and os.path.isdir(src):
                shutil.copytree(src, live, dirs_exist_ok=True, copy_function=link_or_copy)

    def _migrate(self):
        """Replacements from before profiles existed become the GLOBAL profile, so they keep working as before."""
        live = os.path.join(self._mod, 'materials')
        if os.path.isdir(self._profiles) or not os.path.isdir(live):
            return
        os.makedirs(self._pdir(GLOBAL))
        shutil.move(live, os.path.join(self._pdir(GLOBAL), 'materials'))
        if os.path.isdir(os.path.join(self._mod, '_source')):
            shutil.move(os.path.join(self._mod, '_source'), os.path.join(self._pdir(GLOBAL), '_source'))

    def _files(self):
        done = self._profile_files(self._prof()) | self._profile_files(GLOBAL)
        return [[p, SOURCES[s][0], size, 'pending' if p in self._pending else 'applied' if p in done else '']
                for p, (s, _, size) in sorted(self._entries.items())]

    def _pinned(self):
        """Name of the map pinned (made active) in gameinfo.txt, or None."""
        try:
            with open(self._gameinfo, encoding='latin-1', newline='') as f:
                bsp = get_pin(f.read())
        except OSError:
            return None
        return Path(bsp).stem.lower() if bsp else None

    def _write_pin(self, name):
        """Make map `name` the active map in gameinfo.txt (None clears it). latin-1 + newline='' keep other bytes."""
        backup = self._gameinfo + '.texturechanger.bak'
        if not os.path.exists(backup):
            shutil.copy2(self._gameinfo, backup)
        with open(self._gameinfo, encoding='latin-1', newline='') as f:
            text = f.read()
        text = set_pin(text, Path(self._maps[name]).relative_to(self._cstrike).as_posix() if name else None)
        with open(self._gameinfo, 'w', encoding='latin-1', newline='') as f:
            f.write(text)
        os.makedirs(self._mod, exist_ok=True)
        if name:
            Path(self._pin_file).write_text(name)
        else:
            Path(self._pin_file).unlink(missing_ok=True)

    def _preview(self, path):
        source, read, _ = self._entries[path]
        owner = self._owner(path)
        applied = owner is not None
        out = {'path': path, 'source': SOURCES[source][0], 'map': self._current, 'pending': len(self._pending),
               'canRestore': applied, 'global': owner == GLOBAL and self._current is not None}
        try:
            out['orig'] = to_data_url(read_vtf(read()))
        except Exception as e:  # corrupt files or exotic formats
            out['origError'] = str(e)
        try:
            if path in self._pending:
                out['mine'] = self._mine(self._pending[path], 'pending')
            elif applied:
                out['mine'] = self._mine(self._applied_change(path, owner), 'applied')
        except Exception as e:
            out['mineError'] = str(e)
        # This map's own change, but another map is active, so the game doesn't have it right now.
        out['inactive'] = owner == self._current is not None and path not in self._pending and self._pinned() != owner
        out['others'] = [d for d in os.listdir(self._custom)
                         if d.lower() != MOD_NAME and os.path.isfile(os.path.join(self._custom, d, *path.split('/')))]
        return out

    def _change(self, path, name, raw, scale, crop, src=None):
        """A replacement as kept in _pending: the decoded file plus Size/Fit. The VTF is only built on Apply.

        scale is 1/2/4/8 times the original's size, or None to use a .vtf exactly as it is.
        """
        source, read, _ = self._entries[path]
        orig = read_vtf(read())
        vtf_file = name.lower().endswith('.vtf')
        img = read_vtf(raw).get().to_PIL() if vtf_file else Image.open(io.BytesIO(raw))
        img.load()  # fail now on a broken file, not on Apply
        if scale is not None:
            check_replaceable(orig)
        return {'raw': raw, 'name': name, 'img': img, 'orig': orig, 'vtf_file': vtf_file, 'scale': scale,
                'crop': crop, 'source': source, 'profile': self._prof(), 'src': src}

    def _applied_change(self, path, owner):
        """The applied replacement (in profile `owner`) as a change: from its saved source file, else the VTF itself."""
        raw_file, meta_file = self._source_files(path, owner)
        try:
            meta = json.loads(Path(meta_file).read_text())
            c = self._change(path, meta['name'], Path(raw_file).read_bytes(), meta['scale'], meta['crop'], meta.get('src'))
        except (OSError, ValueError, KeyError):  # applied before sources were kept
            c = self._change(path, Path(path).name, Path(self._dest(path, owner)).read_bytes(), None, False)
            o, size = c['orig'], c['img'].size
            c['scale'] = next((k for k in (1, 2, 4, 8) if size == (o.width * k, o.height * k)), None)
        c['saved'] = (c['scale'], c['crop'])  # only Size/Fit can differ from what's applied
        return c

    def _mine(self, c, state):
        """What the page needs to draw a replacement at any Size/Fit by itself: the source image + settings."""
        src = c['img'].convert('RGBA')
        src.thumbnail((768, 768))
        buf = io.BytesIO()
        src.save(buf, 'PNG')
        return {'src': 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode(), 'state': state,
                'scale': c['scale'], 'crop': c['crop'], 'vtfFile': c['vtf_file'],
                'saved': c.get('saved') and {'scale': c['saved'][0], 'crop': c['saved'][1]},
                'asIs': {'w': c['img'].width, 'h': c['img'].height}, 'fmt': vtf_format(src, c['orig'])[0].name,
                'srcPath': c['src']}

    def _stage(self, path, name, raw, scale, src=None):
        self._pending[path] = self._change(path, name, raw, scale, False, src)
        return self._preview(path)

    def _build(self, c):
        if c['scale'] is None:
            return c['raw']
        return make_vtf(c['img'], c['orig'], c['scale'], c['crop'])

    def _apply(self):
        maps = list(dict.fromkeys(c['profile'] for c in self._pending.values() if c['profile'] != GLOBAL))
        for path, c in self._pending.items():
            data = self._build(c)
            dest = self._dest(path, c['profile'])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            Path(dest).unlink(missing_ok=True)  # don't write through a hard link into the game's folder
            Path(dest).write_bytes(data)
            raw_file, meta_file = self._source_files(path, c['profile'])  # keep the file used, for re-sizing later
            os.makedirs(os.path.dirname(raw_file), exist_ok=True)
            Path(raw_file).write_bytes(c['raw'])
            Path(meta_file).write_text(json.dumps({'name': c['name'], 'scale': c['scale'], 'crop': c['crop'], 'src': c['src']}))
        msg = f'Applied {len(self._pending)} texture{"s" * (len(self._pending) != 1)}.'
        self._pending.clear()
        if maps:  # a map's changes are only in the game while it's the active map, so make it active
            target = self._current if self._current in maps else maps[-1]
            if self._pinned() != target:
                self._write_pin(target)
                msg += f' {target} is now the active map.'
            if len(maps) > 1:
                msg += ' Only one map can be active; the other maps keep their changes in their profiles.'
        self._install()
        return msg + ' Restart CS:S to see the changes.'

    # --- called from the UI ---------------------------------------------------------------------------------

    @locked
    def init(self):
        if not self._game:
            return {'needGame': True}
        self._migrate()
        toast = ''
        try:
            wanted = Path(self._pin_file).read_text().strip()
        except OSError:
            wanted = ''
        if wanted and wanted in self._maps and self._pinned() != wanted:  # Steam restored gameinfo.txt
            self._write_pin(wanted)
            toast = f'Steam had reset gameinfo.txt, so {wanted} was made the active map again.'
        self._install()
        try:
            settings = json.loads(Path(SETTINGS).read_text())
        except (OSError, ValueError):
            settings = {}
        return {'game': self._game, 'mod': self._mod, 'maps': sorted(self._maps), 'active': self._pinned(),
                'toast': toast, 'settings': settings, 'profiles': self._profile_names()}

    @locked
    def save_settings(self, settings):
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        Path(SETTINGS).write_text(json.dumps(settings))
        return {}

    @locked
    def pick_game(self):
        res = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not res:
            return {'cancel': True}
        if not os.path.isfile(os.path.join(res[0], 'cstrike', 'gameinfo.txt')):
            return {'error': 'That folder does not contain cstrike/gameinfo.txt. Pick the "Counter-Strike Source" folder.'}
        self._setup(os.path.normpath(res[0]))
        return {'ok': True}

    @locked
    def load(self, name):
        if name:
            self._entries, self._current = map_textures(self._maps[name], self._lookup), name
        else:
            self._entries, self._current = self._stock, None
        return {'map': self._current, 'files': self._files(), 'active': self._pinned(), 'pending': len(self._pending),
                'own': len(self._profile_files(self._prof()))}

    @locked
    def preview(self, path):
        return self._preview(path)

    @locked
    def thumbs(self, paths, stock=False):
        """Small previews for the texture browsers, cached per map (stock=True: base game textures)."""
        entries = self._stock if stock else self._entries
        out = {}
        for p in paths:
            key = (None if stock else self._current, p)
            if key not in self._thumbs and p in entries:
                try:
                    self._thumbs[key] = to_data_url(read_vtf(entries[p][1]()), THUMB)
                except Exception:  # corrupt files or exotic formats: tile shows a placeholder
                    self._thumbs[key] = None
            out[p] = self._thumbs.get(key)
        return out

    @locked
    def other_changes(self):
        """Changes saved on other maps (with a thumbnail of the replacement), for the "Changed first" view."""
        out = []
        for name in self._profile_names():
            if name == self._current:
                continue
            for p in sorted(self._profile_files(name)):
                try:
                    thumb = to_data_url(read_vtf(Path(self._dest(p, name)).read_bytes()), THUMB)
                except Exception:
                    thumb = None
                out.append({'map': name, 'path': p, 'thumb': thumb})
        return {'changes': out}

    @locked
    def game_textures(self):
        """Every base game texture, for the "Choose from game" picker."""
        return {'files': [[p, 'Game', size, ''] for p, (_, _, size) in sorted(self._stock.items())]}

    @locked
    def use_game(self, path, src, from_game=False):
        """Replace `path` with texture `src`, from the base game or from the loaded map's list."""
        entry = self._stock[src] if from_game else self._entries[src]
        return self._stage(path, src.rsplit('/', 1)[-1], entry[1](), 1, src)  # 1x: same size as the original

    @locked
    def choose(self, path):
        res = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=(
            'Images (*.png;*.jpg;*.jpeg;*.tga;*.bmp;*.vtf)', 'All files (*.*)'))
        if not res:
            return {'cancel': True}
        name = Path(res[0]).name
        return self._stage(path, name, Path(res[0]).read_bytes(), None if name.lower().endswith('.vtf') else 1)

    @locked
    def drop(self, path, name, data_url):
        return self._stage(path, name, base64.b64decode(data_url.split(',', 1)[-1]), None if name.lower().endswith('.vtf') else 1)

    @locked
    def set_fit(self, path, scale, crop):
        """Remember Size/Fit (scale 1/2/4/8, or 0 = a .vtf as is). No image work: the page draws the preview.

        Changing an applied replacement makes it a new pending change.
        """
        c = self._pending.get(path) or self._applied_change(path, self._owner(path))
        scale = int(scale) or (None if c['vtf_file'] else 1)
        if scale is not None:
            check_replaceable(c['orig'])
        c['scale'], c['crop'] = scale, bool(crop)
        if c.get('saved') == (c['scale'], c['crop']):
            self._pending.pop(path, None)  # back to what's applied: nothing to do
        else:
            self._pending[path] = c
        return {'pending': len(self._pending)}

    @locked
    def discard(self, path):
        self._pending.pop(path, None)
        return self._preview(path)

    @locked
    def apply(self):
        toast = self._apply()
        return {'toast': toast, 'files': self._files(), 'active': self._pinned(), 'profiles': self._profile_names(),
                'own': len(self._profile_files(self._prof()))}

    @locked
    def restore(self, path):
        owner = self._owner(path)
        for f in (self._dest(path, owner), *self._source_files(path, owner)):
            Path(f).unlink(missing_ok=True)
        self._pending.pop(path, None)
        if owner != GLOBAL and self._pinned() == owner and not self._profile_files(owner):
            self._write_pin(None)  # the active map has no changes left
        self._install()
        return {'files': self._files(), 'active': self._pinned(), 'profiles': self._profile_names(),
                'own': len(self._profile_files(self._prof()))}

    @locked
    def restore_all(self):
        """Undo every change in the current profile: this map's, or the global one."""
        profile = self._prof()
        count = len(self._profile_files(profile))
        shutil.rmtree(self._pdir(profile), ignore_errors=True)
        self._pending = {p: c for p, c in self._pending.items() if c['profile'] != profile}
        if profile != GLOBAL and self._pinned() == profile:
            self._write_pin(None)
        self._install()
        return {'files': self._files(), 'active': self._pinned(), 'profiles': self._profile_names(), 'own': 0,
                'count': count}

    @locked
    def export(self, path):
        res = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=Path(path).stem + '.png',
                                              file_types=('PNG image (*.png)',))
        if not res:
            return {'cancel': True}
        out = res if isinstance(res, str) else res[0]
        read_vtf(self._entries[path][1]()).get().to_PIL().save(out if out.lower().endswith('.png') else out + '.png')
        return {'toast': f'Saved {Path(out).name}'}

    @locked
    def set_active(self, name):
        self._write_pin(name)
        self._install()
        return {'active': self._pinned()}

    @locked
    def open_mod(self):
        os.makedirs(self._mod, exist_ok=True)
        if WINDOWS:
            os.startfile(self._mod)
        else:
            subprocess.Popen(['xdg-open', self._mod])
        return {}

    @locked
    def quit(self, apply_first):
        if apply_first:
            self._apply()
        self._pending.clear()
        self._quitting = True
        threading.Thread(target=self._window.destroy, daemon=True).start()
        return {}

    def _on_closing(self, *_):
        """Ask in the UI about unapplied changes instead of losing them (returning False cancels the close)."""
        if self._quitting or not self._pending:
            return True
        threading.Thread(target=self._window.evaluate_js, args=(f'askQuit({len(self._pending)})',), daemon=True).start()
        return False


def dark_title_bar():
    """Dark Windows title bar matching the page (Windows 11; silently ignored elsewhere)."""
    hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
    if hwnd:
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)  # immersive dark mode
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(ctypes.c_int(0x001A1716)), 4)  # caption colour #16171a


def main():
    api = Api(find_game())
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    window = webview.create_window(TITLE, os.path.join(base, 'ui.html'), js_api=api, width=1440, height=1080,
                                   min_size=(1100, 760), background_color='#16171a')
    api._window = window
    window.events.closing += api._on_closing
    if WINDOWS:
        window.events.shown += dark_title_bar
    webview.start()


if __name__ == '__main__':
    main()
