"""Rolling intervals from confirmed capture times, independent of UI/write latency."""
from collections import deque


class MeteorTiming:
    def __init__(self):
        self.events = deque(maxlen=5)
        self.pauses = []

    def reset(self):
        self.events.clear()

    def add(self, detected_at):
        detected_at=self.active_time(detected_at)
        if self.events and detected_at < self.events[-1]:
            raise ValueError('陨星时间必须按检测顺序递增')
        self.events.append(float(detected_at))

    @property
    def intervals(self):
        times = list(self.events)
        return [b-a for a, b in zip(times, times[1:])]

    @property
    def average(self):
        gaps = self.intervals
        return sum(gaps)/len(gaps) if gaps else None

    def since_last(self, now):
        return max(0., self.active_time(now)-self.events[-1]) if self.events else None

    @property
    def paused(self):
        return bool(self.pauses and self.pauses[-1][1] is None)

    def pause(self, now):
        if not self.paused:self.pauses.append([float(now),None])

    def resume(self, now):
        if self.paused:self.pauses[-1][1]=max(float(now),self.pauses[-1][0])

    def active_time(self, now):
        # Clamp each span to the capture timestamp: a queued pre-pause frame must
        # not lose time spent paused after that frame was captured.
        paused=sum(max(0.,min(now,end if end is not None else now)-start) for start,end in self.pauses)
        return float(now)-paused

    def snapshot(self, now):
        current=self.active_time(now)
        return dict(ages=[max(0.,current-event) for event in self.events],paused=self.paused)

    def restore(self, state, now, elapsed=0.):
        self.events=deque((now-age-elapsed for age in state['ages']),maxlen=5)
        self.pauses=[]
        if state['paused']:self.pause(now)


def duration(seconds):
    if seconds is None:
        return '—'
    total = max(0, int(seconds + .5))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f'{hours}时{minutes:02}分{seconds:02}秒'
    if minutes:
        return f'{minutes}分{seconds:02}秒'
    return f'{seconds}秒'
