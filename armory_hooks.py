"""
N64 Exporter - Armory Hooks

armor_64's entry points into Armory's library hooks (arm/library_hooks.py,
write_data.get_library_hooks()):
  - runtimes(), targets(): "Ares" in the Runtime list (Play) and "Nintendo 64"
    in the exporter's Target list (Build, Publish)
  - owns_build(): those builds are armor_64's
  - compile(), clean(): armor_64 builds them (exporter/export/build_pipeline.py)
    instead of Armory's khamake compile, and cleans only the N64 output
  - clean_project(): Clean Project stops armor_64's compilation server, which
    would keep the build folder locked
  - preferences(), draw_preferences(): the Nintendo 64 settings in Armory's
    preferences (toolchain, MSYS2, Ares...)
  - write_main(): for them, the arm_target_n64 and arm_build_dir defines and
    armor_64's Haxe macro (N64Macro)

The list and preference functions run when Blender draws (or the project's
libraries load): they don't import armor_64's modules (blender.py may not
have registered them yet).
"""

RUNTIME = 'Ares'
TARGET = 'n64'


def runtimes():
    # 2: the number Blender saved "Ares" with before (keeps existing .blend files' choice)
    return [(RUNTIME, 'Ares', 'Nintendo 64, in the Ares emulator', 2)]


def targets():
    return [(TARGET, 'Nintendo 64', 'Nintendo 64 ROM')]


def owns_build(request):
    """Play with the Ares runtime, Build/Publish of a Nintendo 64 exporter entry."""
    if request.is_export:
        return request.exporter_target == TARGET
    return request.is_play and request.runtime == RUNTIME


def compile(request, done):
    from armor64.export import build_pipeline
    build_pipeline.start(request, done)


def clean(request):
    from armor64.export import build_pipeline
    build_pipeline.clean()


def clean_project():
    from armor64.export import build_pipeline
    build_pipeline.clean_project()


def write_main():
    import arm.utils
    from arm.library_hooks import BuildRequest

    if not owns_build(BuildRequest.current()):
        return {}

    import bpy

    build_dir = arm.utils.build_dir()
    # The project's Haxe package (World > Armory Project > Package), as Armory
    # names its Sources folder
    package = arm.utils.safestr(bpy.data.worlds['Arm'].arm_project_package)

    return {
        'defines': [
            'arm_target_n64',
            f'arm_build_dir=../{build_dir}',
        ],
        'parameters': [
            f'--macro armor64.N64Macro.register("{package}")',
        ],
    }


def _path_update(name):
    """Keeps a path setting absolute and without `..`."""
    def update(self, context):
        import bpy
        if getattr(self, 'skip_update', False):
            return
        self.skip_update = True
        setattr(self, name, bpy.path.reduce_dirs([bpy.path.abspath(getattr(self, name))])[0])
        self.skip_update = False
    return update


def preferences():
    from bpy.props import BoolProperty, IntProperty, StringProperty

    def path(name, label, description):
        return StringProperty(name=label, description=description, default='', subtype='FILE_PATH',
                              update=_path_update(name))

    return {
        'n64_toolchain_path': path('n64_toolchain_path', 'N64 Toolchain Path',
                                   'Path to the N64 Toolchain installation directory'),
        'msys2_bash_executable': path('msys2_bash_executable', 'MSYS2 Bash Executable',
                                      'Path to the MSYS2 Bash executable'),
        'mingw64_path': path('mingw64_path', 'MinGW64 Path', 'Path to the MinGW64 directory'),
        'ares_emulator_executable': path('ares_emulator_executable', 'Ares Emulator Executable',
                                         'Path to the Ares Emulator executable'),
        'open_n64_rom_directory': BoolProperty(
            name='Open Nintendo 64 ROM Directory',
            description='Open the Nintendo 64 ROM directory after successfully build',
            default=False),
        'n64_compilation_server_port': IntProperty(
            name='N64 Compilation Server Port',
            description="Port of the Haxe compilation server of Nintendo 64 builds (used when "
                        "Armory's compilation server is on; different from Armory's port)",
            default=6464, min=1024, max=65535),
    }


def draw_preferences(layout, prefs):
    layout.label(text='Nintendo 64 Settings')
    layout.prop(prefs, 'n64_toolchain_path')
    layout.prop(prefs, 'msys2_bash_executable')
    layout.prop(prefs, 'mingw64_path')
    layout.prop(prefs, 'ares_emulator_executable')
    layout.prop(prefs, 'open_n64_rom_directory')
    if getattr(prefs, 'compilation_server', False):
        layout.prop(prefs, 'n64_compilation_server_port')
    layout.label(text='Libraries:')
    row = layout.row(align=True)
    row.operator('n64_exporter.install_libdragon_dependencies', icon='IMPORT')
    row.operator('n64_exporter.build_libdragon', icon='MOD_BUILD')
    row.operator('n64_exporter.build_tiny3d', icon='MOD_BUILD')
