"""
N64 build: what runs after Armory's export for a build armor_64 owns
(armory_hooks.py compile()).

Haxe runs with armor_64's macros, which write the IR (n64_traits.json...);
then the N64 export, make and, for Play, Ares. Haxe only has to type-check:
nothing uses its output. So instead of Armory's khamake compile (shaders for
this computer, the game as C++, a Visual Studio project):
  1. khamake only writes the Haxe build file and the asset list
     (--nohaxe --noshaders --noproject: a fraction of a second when warm)
  2. armor_64 completes the asset list with the shaders as named, empty
     placeholders (Kha builds kha.Shaders from it) and copies Koui's themes
  3. Haxe runs directly, with --no-output; meanwhile the meshes are exported
     (they only need the .blend), and the rest of the export follows Haxe
With Armory's compilation server preference on, Haxe runs through armor_64's
own compilation server (its own port: Armory's serves the other targets),
which keeps the typed modules between builds.
"""

import atexit
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import time
import traceback
from typing import Callable, Optional

import bpy

import arm.library_hooks as library_hooks
import arm.log as log
import arm.utils
import armor64.utils as n64_utils

# Haxe output name: Kha's asset list (<name>-resources) and Koui's themes (<name>) go next to it
OUTPUT = 'n64haxe'

_request = None
_done: Optional[Callable[[bool], None]] = None
_exporter = None  # N64Exporter of the running build (None once its asset export failed)
_server = None  # armor_64's Haxe compilation server process


def start(request, done: Callable[[bool], None]):
    """Build `request` (library_hooks.BuildRequest), then call done(success)."""
    global _request, _done
    _request, _done = request, done
    os.chdir(arm.utils.get_fp())
    cmd = [arm.utils.get_node_path(), arm.utils.get_khamake_path(), '--nohaxe', '--noshaders', '--noproject']
    if not bpy.data.worlds['Arm'].arm_verbose_output:
        cmd.append('--quiet')
    library_hooks.run_process(cmd, _setup_done)


def clean():
    """Publish's clean: the N64 output only. khamake's cache (build/) stays, so
    its next run is warm."""
    n64_dir = os.path.join(arm.utils.build_dir(), 'n64')
    if os.path.isdir(n64_dir):
        shutil.rmtree(n64_dir, onerror=lambda f, p, e: (os.chmod(p, 0o777), f(p)))


def clean_project():
    """Clean Project: armor_64's compilation servers stopped (they work in the
    build folder, so it couldn't be deleted): this session's, and any left
    by a Blender session that crashed."""
    stop_server()
    _stop_orphan_servers(n64_utils.get_n64_compilation_server_port())


def _finish(success: bool):
    done = _done
    if done is not None:
        done(success)


def _setup_done(code: int):
    global _exporter
    if code != 0:
        log.error('khamake failed')
        _finish(False)
        return
    try:
        hxml = _prepare_haxe()
    except Exception as e:
        log.error(f'N64: could not prepare the Haxe build: {e}')
        _finish(False)
        return
    from armor64.exporter import N64Exporter
    _exporter = N64Exporter()
    library_hooks.run_process(_haxe_cmd(hxml), _haxe_done, meanwhile=_export_assets)


def _haxe_cmd(hxml: str) -> list:
    """Haxe compiling `hxml`: through the compilation server when it's on."""
    haxe = arm.utils.get_haxe_path()
    args = ['--cwd', os.path.dirname(hxml), os.path.basename(hxml)]
    # The macros make the game's modules depend on this file (N64MacroBase):
    # changed, the server types them again, so their IR is extracted again
    with open(os.path.join(arm.utils.build_dir(), 'n64_compile.stamp'), 'w') as f:
        f.write(str(time.time_ns()))
    if not arm.utils.get_compilation_server():
        stop_server()
        return [haxe] + args
    port = n64_utils.get_n64_compilation_server_port()
    if not _start_server(haxe, port):
        log.warn(f'N64: the compilation server could not use port {port} (Armory preferences); '
                 'compiling without it')
        return [haxe] + args
    return [haxe, '--connect', str(port)] + args


def _start_server(haxe: str, port: int) -> bool:
    """Start the compilation server (once per Blender session); whether it's
    listening."""
    global _server
    if _server is not None and _server.poll() is None:
        return True
    # Already listening: this port's server from a Blender session that crashed
    if _listening(port):
        return True
    _server = subprocess.Popen([haxe, '--wait', str(port)])
    atexit.register(stop_server)
    # Connecting before it listens would fail
    deadline = time.time() + 5
    while time.time() < deadline and _server.poll() is None:
        if _listening(port):
            return True
        time.sleep(0.05)
    stop_server()  # Exited or not listening
    return False


def _stop_orphan_servers(port: int):
    """Stop the compilation servers on `port` whose Blender isn't running
    anymore (a running one's belongs to another Blender session). Windows only:
    elsewhere a server doesn't lock the folder it works in."""
    if os.name != 'nt':
        return
    query = ("Get-CimInstance Win32_Process -Filter \"Name='haxe.exe'\" | "
             "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.CommandLine)\" }")
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-Command', query],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log.warn(f'N64: could not look for compilation servers left running: {e}')
        return
    for line in out.splitlines():
        parts = line.strip().split('|', 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, parent, command = int(parts[0]), int(parts[1]), parts[2].split()
        if command[-2:] == ['--wait', str(port)] and not _process_running(parent):
            try:
                os.kill(pid, 9)
                log.info(f'N64: stopped a compilation server left by a crashed Blender (process {pid})')
            except OSError as e:
                log.warn(f'N64: could not stop the compilation server left running (process {pid}): {e}')


def _process_running(pid: int) -> bool:
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    running = kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259  # STILL_ACTIVE
    kernel32.CloseHandle(handle)
    return bool(running)


def _listening(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def stop_server():
    global _server
    if _server is not None:
        _server.kill()
        _server = None


def _export_assets() -> bool:
    """The meshes (and what they need), exported while Haxe compiles."""
    global _exporter
    try:
        _exporter.export_assets()
        return True
    except Exception:
        traceback.print_exc()
        log.error('N64: the mesh export failed (see the errors above)')
        _exporter = None
        return False


def _prepare_haxe() -> str:
    """Write armor_64's asset list, themes and Haxe build file next to khamake's;
    returns the build file's path."""
    fp = arm.utils.get_fp()
    kha_build = os.path.join(fp, 'build')
    hxmls = glob.glob(os.path.join(kha_build, 'project-*.hxml'))
    if not hxmls:
        raise RuntimeError(f'no Haxe build file from khamake in {kha_build}')
    kha_hxml = max(hxmls, key=os.path.getmtime)
    platform = os.path.basename(kha_hxml)[len('project-'):-len('.hxml')]

    # Asset list: khamake's (without shaders), plus every shader as an empty placeholder
    resources = os.path.join(kha_build, OUTPUT + '-resources')
    os.makedirs(resources, exist_ok=True)
    with open(os.path.join(kha_build, platform + '-resources', 'files.json'), encoding='utf-8') as f:
        manifest = json.load(f)
    placeholder = 'n64_placeholder'
    open(os.path.join(resources, placeholder), 'wb').close()
    files = [entry for entry in manifest['files'] if entry.get('type') != 'shader']
    for name in sorted(_shader_names(fp, kha_hxml)):
        files.append({'name': name, 'files': [placeholder], 'file_sizes': [0], 'type': 'shader', 'inputs': []})
    manifest['files'] = files
    n64_utils.write_if_changed(os.path.join(resources, 'files.json'), json.dumps(manifest, indent=2))

    # Koui's themes (.ksn), read from the folder named like the output
    assets_dir = os.path.join(kha_build, platform)
    for theme in glob.glob(os.path.join(assets_dir, '**', '*.ksn'), recursive=True):
        dest = os.path.join(kha_build, OUTPUT, os.path.relpath(theme, assets_dir))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(theme, dest)

    # khamake's build file, to armor_64's output and without writing code
    with open(kha_hxml, encoding='utf-8') as f:
        lines = f.read().split('\n')
    out = []
    for line in lines:
        if re.match(r'^-(cpp|js|hl|java|jvm|cs|python|lua|php|neko|swf)\s', line):
            out += [f'-cpp {OUTPUT}-build/Sources', '--no-output']
        else:
            out.append(line)
    hxml = os.path.join(kha_build, OUTPUT + '.hxml')
    n64_utils.write_if_changed(hxml, '\n'.join(out))
    return hxml


def _shader_names(fp: str, kha_hxml: str) -> set:
    """kha.Shaders field names of every shader khamake would compile: the
    project's and the libraries' (their khafile.js addShaders(), relative to
    their folder), and Kha's and libraries' without a khafile.js
    (Sources/Shaders)."""
    sources = []

    def add_khafile_shaders(root: str) -> bool:
        khafile = os.path.join(root, 'khafile.js')
        if not os.path.isfile(khafile):
            return False
        with open(khafile, encoding='utf-8') as f:
            for pattern in re.findall(r'project\.addShaders\(\s*["\']([^"\']+)["\']', f.read()):
                sources.extend(glob.glob(os.path.join(root, pattern), recursive=True))
        return True

    add_khafile_shaders(fp)
    with open(kha_hxml, encoding='utf-8') as f:
        for path in re.findall(r'^-cp\s+(.+?)\s*$', f.read(), re.M):
            path = path.rstrip('/\\')
            if os.path.basename(path) == 'Sources':
                sources.extend(glob.glob(os.path.join(path, 'Shaders', '**', '*.glsl'), recursive=True))
                add_khafile_shaders(os.path.dirname(path))
    names = set()
    for path in sources:
        name = os.path.basename(path)
        if name.endswith('.glsl'):
            names.add(re.sub(r'[.-]', '_', name[:-len('.glsl')]))
    return names


def _haxe_done(code: int):
    exporter = _exporter
    if exporter is None:  # the mesh export failed (and stopped Haxe)
        _finish(False)
        return
    if code != 0:
        exporter.cleanup()
        log.error('N64: Haxe build failed (see the errors above)')
        _finish(False)
        return
    try:
        if _request.is_play:
            success = exporter.play()
        elif _request.is_publish:
            success = exporter.publish()
        else:
            exporter.export()
            success = True
        if success and not _request.is_play and n64_utils.get_open_n64_rom_directory():
            arm.utils.open_folder(os.path.abspath(os.path.join(arm.utils.build_dir(), 'n64')))
    except Exception:
        traceback.print_exc()
        success = False
    _finish(success)
