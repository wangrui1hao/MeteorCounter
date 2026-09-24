"""User data paths, verified legacy migration and protected cleanup."""
import hashlib
import tempfile
import json
import os
import shutil
from datetime import datetime,timedelta
from pathlib import Path

EVIDENCE_LIMIT = 500 * 1024 * 1024


def user_directory():
    return Path(os.environ['LOCALAPPDATA'])/'MeteorCounter'


def import_legacy_data(source, destination):
    """Copy a legacy data directory once, verifying every byte before promotion.

    Existing per-user data always wins; never merge databases or histories.
    The original directory remains as an upgrade backup.
    """
    source=Path(source);destination=Path(destination)
    if (destination/'counts.sqlite3').is_file() or not (source/'counts.sqlite3').is_file():return False
    if destination.exists():
        raise FileExistsError(f'记录目录已存在但没有数据库，未覆盖旧记录：{destination}')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='migration-',dir=destination.parent) as tmp:
        staged=Path(tmp)/'data'
        shutil.copytree(source,staged)
        for original in source.rglob('*'):
            if not original.is_file():continue
            copied=staged/original.relative_to(source)
            with original.open('rb') as a,copied.open('rb') as b:
                if hashlib.file_digest(a,'sha256').digest()!=hashlib.file_digest(b,'sha256').digest():
                    raise OSError(f'旧记录校验失败，未启用新目录：{original.name}')
        staged.rename(destination)
    return True


def maintain_diagnostics(app_directory, protected=(), now=None, limit=EVIDENCE_LIMIT):
    """Expire diagnostic files only; caller serializes this with recorder writes."""
    root=Path(app_directory)
    if any(p.is_symlink() or p.is_junction() for p in (root,root/'data',root/'archive')):
        raise ValueError('拒绝清理外部数据目录链接')
    now=now or datetime.now().astimezone()
    protected={Path(p).resolve() for p in protected}
    bundles=[];total=0;removed=0;log_bytes=0
    for evidence in (root/'data/evidence',root/'archive/evidence'):
        if evidence.is_symlink() or evidence.is_junction():
            raise ValueError('拒绝清理外部截图目录')
        for day in evidence.iterdir() if evidence.exists() else ():
            if not day.is_dir():continue
            if day.is_symlink() or day.is_junction():raise ValueError('拒绝清理外部截图日期目录')
            for folder in day.iterdir():
                if not folder.is_dir():continue
                nodes=[folder]+list(folder.rglob('*'))
                if any(p.is_symlink() or p.is_junction() for p in nodes):
                    raise ValueError('拒绝清理包含外部链接的截图')
                size=sum(p.stat().st_size for p in nodes if p.is_file())
                total+=size
                manifest=folder/'record.json'
                if manifest.resolve() in protected or not manifest.is_file():continue
                record=json.loads(manifest.read_text(encoding='utf-8'))
                stamp=datetime.fromisoformat(record['time']).timestamp()
                failure=record['kind'] in ('bag_rejected','bag_unconfirmed')
                bundles.append((failure,stamp,folder,size))
    remaining=[]
    for failure,stamp,folder,size in bundles:
        if now.timestamp()-stamp >= (7 if failure else 3)*86400:
            remove_directory(folder,root);total-=size;removed+=1
        else:remaining.append((failure,stamp,folder,size))
    # Normal images first, then failed-recognition images; oldest within each.
    for failure,stamp,folder,size in sorted(remaining,key=lambda b:(b[0],b[1])):
        if total<=limit:break
        remove_directory(folder,root);total-=size;removed+=1
    logs=root/'data/logs'
    if logs.is_symlink() or logs.is_junction():
        raise ValueError('拒绝清理外部日志目录')
    for path in logs.glob('*.jsonl'):
        if path.is_symlink() or path.is_junction():raise ValueError('拒绝清理外部日志链接')
        try:day=datetime.strptime(path.stem,'%Y-%m-%d').date()
        except ValueError:continue
        if day<=now.date()-timedelta(days=7):path.unlink()
        else:log_bytes+=path.stat().st_size
    return dict(evidence_bytes=total,log_bytes=log_bytes,removed=removed,limit=limit)


def remove_directory(path, root):
    # Windows package virtualization can resolve children to a different physical
    # AppData tree. Validate the logical boundary and every link within it instead.
    path=Path(os.path.abspath(path));root=Path(os.path.abspath(root))
    if path==root or not path.is_relative_to(root):
        raise ValueError(f'拒绝清理数据目录以外的路径：{path}')
    ancestors=[p for p in path.parents if p.is_relative_to(root)]
    if any(p.is_symlink() or p.is_junction() for p in ancestors+[path]):
        raise ValueError(f'拒绝清理外部目录链接：{path}')
    nodes=[path]+list(path.rglob('*'))
    if any(p.is_symlink() or p.is_junction() for p in nodes):
        raise ValueError(f'拒绝清理包含外部链接的目录：{path}')
    shutil.rmtree(path)


def clear_history(store, current_session):
    if store.latest_round_id()!=current_session:
        raise ValueError('当前轮次已变化，请重新打开清理操作')
    removed=0;retained=0
    # Only complete bundles are removable: an old worker can still be finishing
    # the after-frames of a detection queued before the user started a new round.
    for evidence in (store.directory/'evidence',store.directory.parent/'archive/evidence'):
        if evidence.is_symlink() or evidence.is_junction():raise ValueError('拒绝清理外部截图目录')
        for manifest in sorted(evidence.glob('*/*/record.json')):
            record=json.loads(manifest.read_text(encoding='utf-8'))
            if record['session']==current_session:continue
            after=sum(image['file'].startswith('after_') for image in record['images'])
            if after<record.get('requested_after_frames',0):
                retained+=1;continue
            remove_directory(manifest.parent,store.directory.parent);removed+=1
    rounds=store.clear_history(current_session)
    return dict(rounds=rounds,evidence=removed,incomplete=retained)


def clear_old_cache(app_directory, active_extraction=None, now=None):
    root=Path(app_directory);now=now or datetime.now().astimezone()
    active=Path(active_extraction).resolve() if active_extraction else None
    removed=0
    # A live one-file process uses its extraction directory. Exclude it, and
    # only remove other extraction directories older than 24 hours.
    for path in (root/'cache').glob('_MEI*'):
        if path.resolve()==active or not path.is_dir():continue
        if now.timestamp()-path.stat().st_mtime<86400:continue
        remove_directory(path,root);removed+=1
    logs=root/'data/logs'
    if any(p.is_symlink() or p.is_junction() for p in (root,root/'data',logs)):
        raise ValueError('拒绝清理外部日志目录')
    for path in logs.glob('*.jsonl'):
        try:day=datetime.strptime(path.stem,'%Y-%m-%d').date()
        except ValueError:continue  # Not an application daily log.
        if day>=now.date():continue
        if path.is_symlink() or path.is_junction():raise ValueError('拒绝清理外部日志链接')
        path.unlink();removed+=1
    return removed
