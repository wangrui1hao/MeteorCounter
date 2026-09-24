"""Local, replayable evidence. No network access and no continuous video recording."""
import hashlib
import json
import platform
import threading
import uuid
from functools import wraps
from datetime import datetime
from pathlib import Path

import cv2
from maintenance import maintain_diagnostics


VERSION = '2.6.10'


class EvidenceError(OSError):
    pass


def required_recording(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except EvidenceError:
            raise
        except Exception as error:
            raise EvidenceError(f'识别证据保存失败：{error}') from error
    return wrapped


class EvidenceRecorder:
    def __init__(self, directory, save_images=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.pending = set()
        self.save_images = save_images
        build_info=Path(__file__).parent/'build-info.json'
        if build_info.exists():
            self.build=json.loads(build_info.read_text(encoding='utf-8'))['source_fingerprint'][:16]
        else:
            source=b''.join(p.read_bytes() for p in sorted(Path(__file__).parent.glob('*.py')))
            self.build=hashlib.sha256(source).hexdigest()[:16]

    def set_save_images(self, enabled):
        # Serialize consent changes with all image writes, including after-frames.
        with self.lock:
            self.save_images = bool(enabled)
            if not enabled:
                self.pending.clear()

    @required_recording
    def record(self, kind, session, **details):
        now = datetime.now().astimezone()
        row = dict(time=now.isoformat(timespec='milliseconds'), kind=kind, session=session,
                   version=VERSION, build=self.build, **details)
        path = self.directory / 'logs' / f'{now:%Y-%m-%d}.jsonl'
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as file:
                file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
        return row

    def start(self, session):
        return self.record('round_started', session, python=platform.python_version(), opencv=cv2.__version__)

    def maintain(self):
        with self.lock:
            return maintain_diagnostics(self.directory.parent,self.pending)

    def finish(self, manifest_path):
        # No more after-frames will be written, even if capture was interrupted.
        with self.lock:self.pending.discard(str(Path(manifest_path).resolve()))

    @staticmethod
    def write_image(path, frame):
        # Lossless frames are already cropped to the game recognition region.
        ok, encoded = cv2.imencode('.png', frame)
        if not ok:
            raise OSError('无法编码识别证据图')
        encoded.tofile(str(path))

    @required_recording
    def save(self, kind, session, frames, **details):
        """Return a manifest path, or None when diagnostic images are disabled."""
        with self.lock:
            if not self.save_images:return None
            return self._save(kind,session,frames,**details)

    def _save(self, kind, session, frames, **details):
        now = datetime.now().astimezone()
        folder = self.directory / 'evidence' / f'{now:%Y-%m-%d}' / f'{now:%H%M%S_%f}_{kind}_{uuid.uuid4().hex[:6]}'
        folder.mkdir(parents=True)
        images = []
        for label, frame, metadata in frames:
            path = folder / f'{label}.png'
            self.write_image(path, frame)
            images.append(dict(file=path.name, width=frame.shape[1], height=frame.shape[0], **metadata))
        manifest = dict(time=now.isoformat(timespec='milliseconds'), kind=kind, session=session,
                        version=VERSION, build=self.build, images=images, **details)
        path = folder / 'record.json'
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        if details.get('requested_after_frames',0):self.pending.add(str(path.resolve()))
        self.record(kind, session, evidence=str(path), **details)
        return str(path)

    @required_recording
    def add_frame(self, manifest_path, label, frame, **metadata):
        with self.lock:
            if not self.save_images or str(Path(manifest_path).resolve()) not in self.pending:return
            self._add_frame(manifest_path,label,frame,**metadata)

    def _add_frame(self, manifest_path, label, frame, **metadata):
        path = Path(manifest_path)
        self.write_image(path.parent / f'{label}.png', frame)
        # Only the originating recognition worker owns this manifest.
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['images'].append(dict(file=f'{label}.png', width=frame.shape[1], height=frame.shape[0], **metadata))
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temporary.replace(path)
        if sum(im['file'].startswith('after_') for im in manifest['images'])>=manifest.get('requested_after_frames',0):
            self.finish(manifest_path)


class BagObservationGate:
    """Keep quantities across reopenings; throttle repeated failed captures."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.signature = None
        self.evidence = None
        self.last_rejection = None
        self.last_rejection_time = float('-inf')
        self.sample_signature = None
        self.sample_time = float('-inf')

    def sample_due(self, signature, now):
        if signature!=self.sample_signature or now-self.sample_time>=30:
            self.sample_signature=signature;self.sample_time=now
            return True
        return False

    def changed(self, snapshot):
        signature = tuple(sorted(snapshot.items()))
        if signature == self.signature:
            return False
        self.signature = signature
        return True

    def rejection_due(self, reason, now):
        if reason != self.last_rejection or now-self.last_rejection_time >= 60:
            self.last_rejection, self.last_rejection_time = reason, now
            return True
        return False
