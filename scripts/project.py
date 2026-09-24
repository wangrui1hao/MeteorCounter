"""Shared paths; no dependency on Codex, a user name, or the caller's cwd."""
import hashlib,os,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'src'

def fingerprint():
    digest=hashlib.sha256()
    paths=(list(SRC.rglob('*'))+
           list((ROOT/'scripts').glob('*.py'))+list((ROOT/'scripts').glob('*.ps1'))+
           [ROOT/'scripts/MeteorCounter.spec',ROOT/'requirements.txt',ROOT/'requirements-build.txt',
            ROOT/'LICENSE'])
    for path in sorted(paths):
        if not path.is_file() or '__pycache__' in path.parts:continue
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()

def environment():
    result=dict(os.environ)
    result.pop('PYTHONHOME',None);result.pop('PYTHONPATH',None)
    result.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1',OMP_NUM_THREADS='1')
    return result

NO_WINDOW=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
