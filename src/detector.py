"""Pixel-only banner recognition and time-based event debouncing."""
from dataclasses import dataclass
from pathlib import Path
import cv2
import numpy as np

from image_matching import read_image, feature, feature_integrals, stable_match_peak, precise_correlation


@dataclass
class Match:
    score: float
    box: tuple | None
    scale: float = 1.0
    text_score: float = 0.
    star_fraction: float = 0.


class BannerDetector:
    def __init__(self, asset_dir=None, template_name='text.png'):
        asset_dir = Path(asset_dir or Path(__file__).parent / 'assets')
        template = read_image(asset_dir / template_name)
        self.templates = []
        for scale in (0.80, 0.90, 0.96, 1.0, 1.04, 1.10, 1.20):
            resized = cv2.resize(template, None, fx=scale, fy=scale)
            self.templates.append((scale, feature(resized)))

    @staticmethod
    def region(width, height):
        return (int(width * .20), int(height * .055),
                int(width * .80), int(height * .32))

    def detect(self, frame, full_height=None, origin=(0, 0)):
        """Accept full client image, or an already-cropped region and client height."""
        if full_height is None:
            full_height = frame.shape[0]
            x1, y1, x2, y2 = self.region(frame.shape[1], frame.shape[0])
            frame = frame[y1:y2, x1:x2]
            origin = (x1, y1)
        if frame.size == 0 or full_height < 100:
            return Match(0., None)
        ratio = 1080. / full_height
        image = cv2.resize(frame, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
        feat = feature(image)
        return self.detect_prepared(image,feat,ratio,origin)

    def detect_prepared(self,image,feat,ratio,origin,integrals=None):
        if integrals is None:integrals=feature_integrals(feat)
        best = Match(0., None)
        for scale, template in self.templates:
            h, w = template.shape
            if h > feat.shape[0] or w > feat.shape[1]:
                continue
            # Precise validation requires .68; leave a wide numerical margin
            # while discarding plainly unrelated candidates before variance work.
            score,loc = stable_match_peak(feat, template,integrals,minimum=.60)
            if np.isfinite(score) and score > best.score:
                x, y = loc
                patch=feat[y:y+h,x:x+w]
                score=precise_correlation(patch,template)
                # Both halves must be the notification lettering, not a similar edge.
                split=w//2
                halves=min(precise_correlation(patch[:,:split],template[:,:split]),
                           precise_correlation(patch[:,split:],template[:,split:]))
                sx1=max(0,x-round(145*scale));sx2=max(0,x-round(25*scale))
                sy1=max(0,y-round(30*scale));sy2=min(image.shape[0],y+round(80*scale))
                star=image[sy1:sy2,sx1:sx2]
                yellow=0.
                if star.size:
                    hsv=cv2.cvtColor(star,cv2.COLOR_BGR2HSV)
                    yellow=float(np.mean((hsv[:,:,0]>=17)&(hsv[:,:,0]<=44)&(hsv[:,:,1]>=65)&(hsv[:,:,2]>=150)))
                verified=score>=.68 and halves>=.62 and yellow>=.045
                if not verified:continue
                best = Match(float(score),
                    (round(x / ratio + origin[0]), round(y / ratio + origin[1]),
                     round(w / ratio), round(h / ratio)), scale,float(score),yellow)
        return best


@dataclass
class SceneMatch:
    meteor: Match
    shower: Match
    tip_score: float=0.


class EventDetector:
    """Separate banner lettering; share the expensive input normalization."""
    region=staticmethod(BannerDetector.region)

    def __init__(self,asset_dir=None):
        assets=Path(asset_dir or Path(__file__).parent/'assets')
        self.meteor=BannerDetector(assets)
        self.shower=BannerDetector(assets,'text_shower.png')
        tip=read_image(assets/'tip_occlusion.png')
        # Native font rasterization changes thin strokes. Smooth both sides of
        # the tip comparison without changing banner features or thresholds.
        self.tips=[cv2.GaussianBlur(feature(cv2.resize(tip,None,fx=s,fy=s)),(0,0),1.)
                   for s in (1.,.9,1.1,.8,1.2)]

    def detect(self,frame,full_height=None,origin=(0,0)):
        if full_height is None:
            full_height=frame.shape[0]
            x1,y1,x2,y2=self.region(frame.shape[1],full_height)
            frame=frame[y1:y2,x1:x2];origin=(x1,y1)
        if not frame.size or full_height<100:return SceneMatch(Match(0.,None),Match(0.,None))
        ratio=1080./full_height
        image=cv2.resize(frame,None,fx=ratio,fy=ratio,interpolation=cv2.INTER_AREA)
        feat=feature(image)
        integrals=feature_integrals(feat)
        meteor=self.meteor.detect_prepared(image,feat,ratio,origin,integrals)
        shower=self.shower.detect_prepared(image,feat,ratio,origin,integrals)
        tip_score=0.
        if max(meteor.score,shower.score)<.8:
            tip_feat=cv2.GaussianBlur(feat,(0,0),1.)
            tip_integrals=feature_integrals(tip_feat)
            for template in self.tips:
                h,w=template.shape
                if h>feat.shape[0] or w>feat.shape[1]:continue
                candidate,(x,y)=stable_match_peak(tip_feat,template,tip_integrals,minimum=.78)
                if candidate<.78:continue
                score=precise_correlation(tip_feat[y:y+h,x:x+w],template)
                tip_score=max(tip_score,score)
                if tip_score>=.86:break
        return SceneMatch(meteor,shower,tip_score)


class EventGate:
    """Count a sustained appearance once; rearm after sustained visual absence.

    Unknown/missing captures cannot prove absence and never rearm the detector.
    Only time spent in consecutive valid samples contributes to release.
    """
    def __init__(self, threshold=.72, release_seconds=.80, active=False):
        self.threshold = threshold
        self.release_seconds = release_seconds
        self.active = active
        self.candidate_start = None
        self.candidate_frames = 0
        self.absent_start = None
        self.last_sample = None
        self.last_positive = None
        self.candidate_box = None

    def unknown(self):
        self.candidate_start = self.absent_start = self.last_sample = None
        self.candidate_frames = 0
        self.last_positive = None
        self.candidate_box = None

    def occluded(self, now):
        """A recognized in-game tip cannot prove the underlying banner disappeared."""
        if self.last_positive is not None and now-self.last_positive>5:
            self.candidate_start=None;self.candidate_frames=0;self.last_positive=None
            self.candidate_box=None
        self.last_sample=now
        self.absent_start=None

    @staticmethod
    def same_banner(a,b):
        # Relative tolerances work at any resolution. Compare against the first
        # confirming box so gradual drift cannot chain unrelated matches.
        ax,ay,aw,ah=a;bx,by,bw,bh=b
        if min(aw,ah,bw,bh)<=0:return False
        return (min(aw,bw)/max(aw,bw)>=.85 and min(ah,bh)/max(ah,bh)>=.85
                and abs((ax+aw/2)-(bx+bw/2))<=max(4,min(ah,bh))
                and abs((ay+ah/2)-(by+bh/2))<=max(4,min(ah,bh)))

    def update(self, score, now, box=None):
        if self.last_sample is not None and (now < self.last_sample or now-self.last_sample > .55):
            self.unknown()
        self.last_sample = now
        if self.active:
            if score >= self.threshold - .13:
                self.absent_start = None
            elif self.absent_start is None:
                self.absent_start = now
            elif now - self.absent_start >= self.release_seconds:
                self.active = False
                self.absent_start = None
            return False
        if score >= self.threshold:
            if box is not None and self.candidate_box is not None and not self.same_banner(self.candidate_box,box):
                self.candidate_start=None;self.candidate_frames=0;self.candidate_box=None
            self.last_positive=now
            if self.candidate_start is None:
                self.candidate_start = now
                self.candidate_box=box
            self.candidate_frames += 1
            if self.candidate_frames >= 3 and now-self.candidate_start >= .15:
                self.active = True
                self.candidate_start = None
                self.candidate_frames = 0
                self.candidate_box = None
                return True
        else:
            self.candidate_start = None
            self.candidate_frames = 0
            self.candidate_box = None
        return False


class EventTracker:
    def __init__(self,meteor_active=False,shower_active=False,threshold=.8):
        self.meteor=EventGate(threshold=threshold,active=meteor_active)
        self.shower=EventGate(threshold=threshold,active=shower_active)

    def unknown(self):
        self.meteor.unknown();self.shower.unknown()

    def update(self,scene,now):
        threshold=self.meteor.threshold
        if scene.meteor.score>=threshold and scene.shower.score>=threshold:
            self.meteor.occluded(now);self.shower.occluded(now)
            return None
        if scene.tip_score>=.82:
            self.meteor.occluded(now);self.shower.occluded(now)
            return None
        ordinary=self.meteor.update(scene.meteor.score,now,scene.meteor.box)
        rare=self.shower.update(scene.shower.score,now,scene.shower.box)
        return 'shower' if rare else ('meteor' if ordinary else None)
