"""Move completed, quiet evidence bundles outside the project directory."""
import argparse,hashlib,json,time,sys
from datetime import datetime,timezone
from pathlib import Path
from project import ROOT
sys.path.insert(0,str(ROOT/'src'))
from maintenance import user_directory

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--destination',type=Path,default=user_directory()/'archive')
    args=parser.parse_args()
    source=user_directory()/'data/evidence';archive=args.destination.resolve()
    assert not source.is_symlink() and not source.is_junction()
    assert archive!=source and not archive.is_relative_to(source) and not source.is_relative_to(archive)
    assert archive.drive.lower()==source.drive.lower(),'Use the same drive for atomic directory moves.'
    assert not archive.is_symlink() and not archive.is_junction()
    archive.mkdir(parents=True,exist_ok=True)
    (ROOT/'artifacts').mkdir(parents=True,exist_ok=True)
    cutoff=time.time()-300;count=0;total=0;skipped=0
    for day in sorted(source.iterdir()) if source.exists() else []:
        if not day.is_dir():continue
        datetime.strptime(day.name,'%Y-%m-%d')
        assert day.resolve().parent==source.resolve() and not day.is_junction()
        for folder in sorted(day.iterdir()):
            if not folder.is_dir():continue
            assert folder.resolve().parent==day.resolve() and not folder.is_junction()
            files=list(folder.rglob('*'));manifest=folder/'record.json'
            if any(p.is_symlink() or p.is_junction() or not p.resolve().is_relative_to(folder.resolve()) for p in files):
                raise ValueError(f'Unexpected link: {folder}')
            files=[p for p in files if p.is_file()]
            # No records newer than five minutes, incomplete bundles or pending
            # after-frame writes. The running recorder is left undisturbed.
            if not manifest.is_file() or not files or any(p.suffix=='.tmp' or p.stat().st_mtime>cutoff for p in files):
                skipped+=1;continue
            record=json.loads(manifest.read_text(encoding='utf-8'))
            if not record.get('images') or any(not (folder/entry['file']).is_file() for entry in record['images']):
                skipped+=1;continue
            if sum(im['file'].startswith('after_') for im in record['images'])<record.get('requested_after_frames',0):
                skipped+=1;continue
            target=archive/'evidence'/day.name/folder.name
            target.parent.mkdir(parents=True,exist_ok=True)
            assert target.resolve().is_relative_to(archive) and not target.exists()
            before={p.relative_to(folder).as_posix():p.stat().st_size for p in files}
            row=dict(source=str(folder),destination=str(target),files=len(files),bytes=sum(before.values()),
                     record_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),archived_at=datetime.now(timezone.utc).isoformat())
            # Final race check immediately before same-volume rename.
            if any(p.stat().st_mtime>cutoff for p in files):skipped+=1;continue
            folder.rename(target)
            after={p.relative_to(target).as_posix():p.stat().st_size for p in target.rglob('*') if p.is_file()}
            assert before==after
            assert hashlib.sha256((target/'record.json').read_bytes()).hexdigest()==row['record_sha256']
            with (archive/'archive-index.jsonl').open('a',encoding='utf-8') as index:
                index.write(json.dumps(row,ensure_ascii=False)+'\n')
            count+=1;total+=row['bytes']
        # Keep day folders: the active program may append to today's directory.
    result=dict(destination=str(archive),bundles=count,bytes=total,mib=round(total/1024**2,1),
                recent_or_incomplete_bundles_kept=skipped,statistics_untouched=True)
    (ROOT/'artifacts/evidence-archive.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
