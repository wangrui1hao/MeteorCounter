"""Build a single EXE; preserve per-user and legacy data."""
import argparse,importlib.metadata,json,os,shutil,subprocess,sys,tempfile,uuid
from datetime import datetime,timezone
from pathlib import Path
from project import ROOT,SRC,fingerprint,environment,NO_WINDOW
sys.path.insert(0,str(SRC))
from diagnostics import VERSION

def command(arguments,log,timeout=240,env=None,cwd=ROOT):
    result=subprocess.run([str(value) for value in arguments],cwd=cwd,env=env or environment(),
                          creationflags=NO_WINDOW,capture_output=True,timeout=timeout)
    output=result.stdout.decode('utf-8',errors='replace')+'\n'+result.stderr.decode('utf-8',errors='replace')
    log.write_text(output,encoding='utf-8')
    if result.returncode:raise RuntimeError(f'Command failed; see {log}\n{output[-2500:]}')

def require_closed():
    query="[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Get-CimInstance Win32_Process -Filter \"Name = 'MeteorCounter.exe' OR Name = '陨星计数器.exe'\" | Select-Object -ExpandProperty ExecutablePath"
    result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',query],
        capture_output=True,creationflags=NO_WINDOW,timeout=20)
    if result.returncode:raise RuntimeError('Unable to check whether the counter is running.')
    paths={line.strip().casefold() for line in result.stdout.decode('utf-8-sig').splitlines()}
    targets=[ROOT/'runtime'/'陨星计数器.exe',ROOT/'publish'/'陨星计数器.exe']
    if any(str(path).casefold() in paths for path in targets):
        raise RuntimeError('请自行退出正在运行的计数器，再重新发布。没有结束任何进程。')

def licenses(destination):
    destination.mkdir(parents=True)
    shutil.copy2(ROOT/'LICENSE',destination/'MeteorCounter-LICENSE.txt')
    for name in ('numpy','opencv-python-headless','pillow','mss','pyinstaller',
                 'pyinstaller-hooks-contrib','altgraph','packaging','pefile','pywin32-ctypes','setuptools'):
        dist=importlib.metadata.distribution(name)
        for entry in dist.files or ():
            if any(token in entry.name.lower() for token in ('license','copying','notice')):
                file=Path(dist.locate_file(entry))
                if file.is_file():
                    relative=Path(str(entry))
                    if '..' in relative.parts:continue
                    target=destination/name/relative;target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(file,target)
    for relative in ('LICENSE.txt','tcl/tcl8.6/license.terms','tcl/tk8.6/license.terms'):
        file=Path(sys.base_prefix)/relative
        if file.is_file():
            target=destination/'Python'/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(file,target)

def install(package,destination):
    """Install the EXE and retire the old _internal directory, with rollback."""
    destination=destination.resolve()
    assert destination.parent==ROOT and destination.name=='publish'
    assert not destination.is_symlink() and not destination.is_junction()
    destination.mkdir(exist_ok=True)
    backup=ROOT/'build/backups'/(datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8])
    backup.mkdir(parents=True)
    moved=[];installed=[]
    with tempfile.TemporaryDirectory(prefix='install-',dir=ROOT/'build') as tmp:
        staged=Path(tmp).resolve();assert staged.parent==ROOT/'build'
        shutil.copy2(package/'陨星计数器.exe',staged/'陨星计数器.exe')
        try:
            for name in ('陨星计数器.exe','_internal'):
                old=destination/name
                if old.is_symlink() or old.is_junction():raise RuntimeError('Unexpected program link')
                if old.exists():old.rename(backup/name);moved.append(name)
                if name=='陨星计数器.exe':
                    (staged/name).rename(old);installed.append(name)
        except Exception:
            for name in reversed(installed):(destination/name).rename(staged/name)
            for name in reversed(moved):(backup/name).rename(destination/name)
            raise
    if not moved:backup.rmdir()

def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    (ROOT/'build').mkdir(exist_ok=True)
    (ROOT/'artifacts').mkdir(exist_ok=True)
    require_closed()
    if (ROOT/'publish/data').exists():raise RuntimeError('publish 内已有使用记录。请先将其保存到发布目录外，再发布；不会删除这些记录。')
    with tempfile.TemporaryDirectory(prefix='release-',dir=ROOT/'build') as temporary:
        staging=Path(temporary).resolve();assert staging.parent==ROOT/'build'
        resources=staging/'resources'
        licenses(resources/'licenses')
        build_info=dict(version=VERSION,source_fingerprint=fingerprint(),built_at=datetime.now(timezone.utc).isoformat())
        (resources/'build-info.json').write_text(json.dumps(build_info,indent=2),encoding='utf-8')
        build_env=environment();build_env['METEOR_BUILD_RESOURCES']=str(resources)
        print('Building Windows executable...',flush=True)
        command([sys.executable,'-m','PyInstaller','--noconfirm',
                 '--distpath',staging/'dist','--workpath',ROOT/'build/pyinstaller',
                 ROOT/'scripts/MeteorCounter.spec'],ROOT/'build/pyinstaller.log',env=build_env)
        package=staging/'dist'
        assert {path.name for path in package.iterdir()}=={'陨星计数器.exe'}
        assert not (package/'data').exists()
        require_closed()
        install(package,ROOT/'publish')
        result=dict(**build_info,published_to=str(ROOT/'publish'),entries=['陨星计数器.exe'],
                    bytes=(package/'陨星计数器.exe').stat().st_size,no_user_data=True)
        (ROOT/'artifacts/release.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(f'Ready: {ROOT / "publish"}',flush=True)

if __name__=='__main__':main()
