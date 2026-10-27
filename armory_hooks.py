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
  - write_main(): for them, the arm_target_n64 and arm_build_dir defines and
    armor_64's Haxe macro (N64Macro)

The list functions run when Blender draws the lists: they don't import
armor_64's modules (blender.py may not have registered them yet).
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
    from arm.n64.export import build_pipeline
    build_pipeline.start(request, done)


def clean(request):
    from arm.n64.export import build_pipeline
    build_pipeline.clean()


def clean_project():
    from arm.n64.export import build_pipeline
    build_pipeline.clean_project()


def write_main():
    import arm.utils
    from arm.library_hooks import BuildRequest

    if not owns_build(BuildRequest.current()):
        return {}

    build_dir = arm.utils.build_dir()

    return {
        'defines': [
            'arm_target_n64',
            f'arm_build_dir=../{build_dir}',
        ],
        'parameters': [
            '--macro armor64.N64Macro.register()',
        ],
    }
