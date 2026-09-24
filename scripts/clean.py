"""Remove reproducible caches only. Never visits publish or runtime/data."""
import json,shutil
from pathlib import Path
from project import ROOT

def remove_generated(path):
    path=Path(path)
    if not path.exists():return 0
    resolved=path.resolve()
    if resolved==ROOT or not resolved.is_relative_to(ROOT):
        raise ValueError(f'Cleanup target is outside the project: {path}')
    nodes=[path]+list(path.rglob('*')) if path.is_dir() else [path]
    if any(p.is_symlink() or p.is_junction() or not p.resolve().is_relative_to(ROOT) for p in nodes):
        raise ValueError(f'Cleanup target contains a link: {path}')
    size=sum(p.stat().st_size for p in nodes if p.is_file())
    if path.is_dir():shutil.rmtree(path)
    else:path.unlink()
    return size

def clean():
    # build contains only reproducible build outputs and temporary diagnostics.
    # Durable reports belong in artifacts, executable output in publish.
    paths=[ROOT/'build']
    for name in ('src','scripts'):
        root=ROOT/name
        if root.is_dir():paths.extend(root.rglob('__pycache__'))
    removed=[];total=0
    for path in paths:
        if not path.exists():continue
        # Only these generated names are eligible, even if this function is
        # reused by a future release command.
        assert path.name=='__pycache__' or path==ROOT/'build'
        size=remove_generated(path);total+=size
        removed.append(dict(path=path.relative_to(ROOT).as_posix(),bytes=size))
    return dict(removed_bytes=total,removed=removed,personal_data_untouched=True)

if __name__=='__main__':
    result=clean()
    (ROOT/'artifacts').mkdir(exist_ok=True)
    (ROOT/'artifacts/cleanup.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'removed_mib':round(result['removed_bytes']/1024**2,2),'paths':len(result['removed']),
                      'personal_data_untouched':True},ensure_ascii=False))
