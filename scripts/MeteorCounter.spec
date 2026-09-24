# Single-file Windows package. Runtime data never lives in this bundle.
import os
from pathlib import Path
from PIL import _plugins

project=Path(SPECPATH).parent
resources=Path(os.environ['METEOR_BUILD_RESOURCES'])
analysis=Analysis(
    [str(project/'src/app.py')],
    pathex=[str(project/'src')],
    datas=[(str(project/'src/assets'),'assets'),
           (str(resources/'licenses'),'licenses'),
           (str(resources/'build-info.json'),'.')],
    # Runtime images are PNG icons; ICO/BMP are retained for Windows icons.
    excludes=['PIL.'+plugin for plugin in _plugins
              if plugin not in ('PngImagePlugin','IcoImagePlugin','BmpImagePlugin')]
             +['numpy.random','numpy.fft','numpy.polynomial'],
)
# The app only captures still pixels and does not decode video.
analysis.binaries=[entry for entry in analysis.binaries
                   if not Path(entry[0]).name.startswith('opencv_videoio_ffmpeg')]
# All application date/time handling is Python-side. Tcl's world timezone
# database is not used; omit its hundreds of files from each startup extraction.
analysis.datas=[entry for entry in analysis.datas
                if not entry[0].replace('\\','/').startswith('_tcl_data/tzdata/')]
archive=PYZ(analysis.pure)
exe=EXE(
    archive,analysis.scripts,analysis.binaries,analysis.datas,[],
    name='陨星计数器',console=False,upx=False,
    icon=str(project/'src/assets/app.ico'),
    version=str(project/'src/windows_version.txt'),
    runtime_tmpdir=r'%LOCALAPPDATA%\MeteorCounter\cache',
)
