"""Local icon recognition + font-template digit OCR; no game state access."""
from pathlib import Path
from dataclasses import dataclass
import hashlib
import cv2
import numpy as np
from image_matching import read_image, feature_integrals, stable_match_peak, precise_correlation
from collections import OrderedDict


def glyph_normalize(mask):
    return cv2.resize(mask,(24,32),interpolation=cv2.INTER_AREA).astype(np.float32)/255


def icon_histogram(image):
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
    mask=((hsv[:,:,1]>45)&(hsv[:,:,2]>55)).astype(np.uint8)*255
    hist=cv2.calcHist([hsv],[0,1],mask,[24,8],[0,180,0,256])
    cv2.normalize(hist,hist,1,0,cv2.NORM_L1)
    return hist


class DigitReader:
    def __init__(self, assets):
        self.glyphs={str(i):[] for i in range(10)}
        for path in assets.glob('digit_*.png'):
            self.glyphs[path.stem.split('_')[1]].append(glyph_normalize(read_image(path,cv2.IMREAD_GRAYSCALE)))
        self.glyph_arrays={ch:np.stack(refs) for ch,refs in self.glyphs.items() if refs}
        self.cache=OrderedDict()

    def read(self, patch):
        gray=cv2.cvtColor(patch,cv2.COLOR_BGR2GRAY)
        # Exact grayscale pixels only: repeated reads cannot reuse stale counts.
        key=(gray.shape,gray.tobytes())
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        result=self.read_gray(gray)
        self.cache[key]=result
        if len(self.cache)>128:self.cache.popitem(last=False)
        return result

    def read_gray(self, gray):
        if gray.min()==gray.max():return None,0.
        primary=self._read(gray,100)
        if primary[0] is not None:return primary
        # Native small windows have fewer antialiased pixels per stroke. A fixed
        # cut can erase their edges even after normalizing the game to 1080p.
        # Estimate foreground/background separation from this quantity strip;
        # retain strict glyph/margin checks and require agreeing whole numbers.
        split=cv2.threshold(gray,0,255,cv2.THRESH_BINARY|cv2.THRESH_OTSU)[0]
        thresholds={85,90,105,110,115,125}
        # Sample both sides of the antialiasing transition: one successful cut
        # alone cannot satisfy the independent whole-number agreement below.
        thresholds.update(int(np.clip(split+offset,55,180)) for offset in (-16,-8,0,8,16))
        # Selected quantities are bright white, while other cards use gray.
        # Derive additional cuts from the strip's own foreground contrast.
        background=float(np.median(gray[gray<=split]))
        foreground=float(np.median(gray[gray>split]))
        # Thin native strokes can need lower cuts, and a valid antialiasing
        # interval may span only a few gray levels. Sample the same contrast
        # range finely enough for independent whole-number agreement.
        contrast=foreground-background
        thresholds.update(range(round(background+.25*contrast),round(background+.85*contrast)+1))
        thresholds.discard(100)
        alternatives=[self._read(gray,t) for t in sorted(thresholds)]
        valid=[r for r in alternatives if r[0] is not None]
        if len(valid)>=2 and len({r[0] for r in valid})==1:
            return valid[0][0],min(r[1] for r in valid)
        return primary

    def _read(self, gray, threshold):
        mask=(gray>threshold).astype(np.uint8)*255
        # The normalized quantity text is inset from the card's left edge.
        # Remove that edge before it can join the x prefix on five-digit counts.
        mask[:,:6]=0
        _,_,stats,_=cv2.connectedComponentsWithStats(mask)
        # A rounded card border can join the x prefix below the text baseline.
        # Find that baseline from interior digit components before segmenting.
        interior=[b for b in stats[1:] if b[4]>30 and 17<=b[3]<25 and
                  4<b[2]<24 and 2<b[1]<20 and 12<b[0] and b[0]+b[2]<gray.shape[1]-2]
        if interior:
            baseline=max(b[1]+b[3] for b in interior)
            mask[baseline:]=0
            _,_,stats,_=cv2.connectedComponentsWithStats(mask)
        boxes=sorted([tuple(map(int,b[:4])) for b in stats[1:]
            if b[4]>12 and 8<b[3]<25 and 4<b[2]<24 and 2<b[1]<20
            and b[0]>3 and b[0]+b[2]<gray.shape[1]],key=lambda b:b[0])
        if not 2<=len(boxes)<=6: return None,0.
        # Require a short x prefix, evenly spaced full-height digits and a shared baseline.
        prefix=boxes[0]; digits=boxes[1:]
        # Quantities are right aligned; a merged/missing trailing digit must
        # never turn 11044 into 110 or 75 into 7.
        if digits[-1][0]+digits[-1][2]<115:return None,0.
        if prefix[3]>16 or any(b[3]<17 for b in digits):return None,0.
        if max(b[1]+b[3] for b in boxes)-min(b[1]+b[3] for b in boxes)>3:return None,0.
        if any(not 14<=b[0]-a[0]<=23 for a,b in zip(boxes,boxes[1:])):return None,0.
        value=''; quality=1.
        for x,y,w,h in digits:
            glyph=glyph_normalize(mask[y:y+h,x:x+w])
            scores=sorted([(1-float(np.min(np.mean(np.abs(refs-glyph),axis=(1,2)))),ch)
                           for ch,refs in self.glyph_arrays.items()],reverse=True)
            best,ch=scores[0]; margin=best-scores[1][0]
            if best<.88 or margin<.035:return None,best
            value+=ch;quality=min(quality,best)
        if len(value)>1 and value.startswith('0'):return None,0.
        return int(value),quality


@dataclass
class BallRead:
    key: str | None
    amount: int
    icon: np.ndarray
    slot: int
    quality: float
    icon_quality: float=0.


@dataclass
class BagRead:
    visible: bool
    valid: bool
    balls: list
    compact: bool=False
    reason: str=''
    header_score: float=0.
    layout: dict | None=None


class InventoryDetector:
    def __init__(self, custom_dir=None):
        self.assets=Path(__file__).parent/'assets'/'bag'
        self.header=read_image(self.assets/'header.png')
        self.header_templates={}
        self.reader=DigitReader(self.assets)
        self.catalog={}
        self.icon_features={}
        self.icon_cache=OrderedDict()
        self.aliases={}
        for p in sorted(self.assets.glob('ball_*.png')):self.catalog[p.stem]=read_image(p)
        self.custom_dir=Path(custom_dir) if custom_dir else None
        if self.custom_dir and self.custom_dir.exists():
            for p in self.custom_dir.glob('custom_*.png'):
                icon=read_image(p)
                key,_=self.identify(icon,core=icon)
                if key:self.aliases[p.stem]=key
                else:
                    self.catalog[p.stem]=icon
                    self.icon_cache.clear()

    def identify(self, patch, core=None):
        # Exact pixels only: changing either the match area or the histogram
        # core must be evaluated again. Quantities are read separately.
        cache_key=(patch.shape,patch.dtype.str,patch.tobytes(),
                   None if core is None else (core.shape,core.dtype.str,core.tobytes()))
        if cache_key in self.icon_cache:
            self.icon_cache.move_to_end(cache_key)
            return self.icon_cache[cache_key]
        result=self._identify(patch,core)
        self.icon_cache[cache_key]=result
        if len(self.icon_cache)>64:self.icon_cache.popitem(last=False)
        return result

    def _identify(self, patch, core=None):
        scores=[]
        colors=[]
        hist=icon_histogram(core if core is not None else patch)
        for key,icon in self.catalog.items():
            if key not in self.icon_features:
                self.icon_features[key]=([cv2.resize(icon,None,fx=s,fy=s) for s in (.94,1.,1.06)],icon_histogram(icon))
            templates,reference_hist=self.icon_features[key]
            best=0
            for t in templates:
                if t.shape[0]>patch.shape[0] or t.shape[1]>patch.shape[1]:continue
                score=cv2.minMaxLoc(cv2.matchTemplate(patch,t,cv2.TM_CCOEFF_NORMED))[1]
                best=max(best,score)
            scores.append((best,key))
            colors.append((cv2.compareHist(hist,reference_hist,cv2.HISTCMP_BHATTACHARYYA),key,best))
        scores.sort(reverse=True)
        colors.sort()
        # Rotation/hover and a small game-rendered cursor change pixels, but not the ball's color distribution.
        if colors and colors[0][0]<.23 and colors[0][2]>.45 and (len(colors)==1 or colors[1][0]-colors[0][0]>.13):
            return colors[0][1],1-colors[0][0]
        if scores and scores[0][0]>=.78 and (len(scores)==1 or scores[0][0]-scores[1][0]>=.07):
            return scores[0][1],scores[0][0]
        return None,scores[0][0] if scores else 0

    @staticmethod
    def region(width,height):
        # Wider layouts can add columns while keeping the details panel fixed-width.
        return (0,0,round(width*.82),round(height*.88))

    @staticmethod
    def normalize(frame, full_height, scale=1.):
        ratio=1080/(full_height*scale)
        # AREA is for downsampling. Linear interpolation preserves antialiased
        # digit strokes when enlarging a small native capture.
        method=cv2.INTER_LINEAR if ratio>1 else cv2.INTER_AREA
        return cv2.resize(frame,None,fx=ratio,fy=ratio,interpolation=method)

    @staticmethod
    def header_gray(image):
        # The opaque header is stable scenery. Suppress subpixel font-edge
        # differences between native resolutions before comparing its parts.
        gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY).astype(np.float32)
        return cv2.GaussianBlur(gray,(0,0),1.5)

    def match_header(self,image):
        """One title template for all resolutions; measure UI scale from pixels.

        Client height is only the initial scale estimate. Aspect ratios and UI
        scaling can change it, so search template sizes when that estimate fails.
        Full title, category text and bag icon must independently pass throughout.
        """
        smooth=self.header_gray(image[:220])
        integrals=feature_integrals(smooth)
        def match(scale):
            if scale not in self.header_templates:
                self.header_templates[scale]=self.header_gray(cv2.resize(self.header,None,fx=scale,fy=scale))
            template=self.header_templates[scale]
            h,w=template.shape
            if h>smooth.shape[0] or w>smooth.shape[1]:return None
            score,position=stable_match_peak(smooth,template,integrals)
            x,y=position;patch=smooth[y:y+h,x:x+w]
            category_slice=(slice(round(28*scale),round(70*scale)),slice(round(85*scale),w))
            icon_slice=(slice(0,round(72*scale)),slice(0,round(85*scale)))
            category=precise_correlation(patch[category_slice],template[category_slice])
            icon=precise_correlation(patch[icon_slice],template[icon_slice])
            return dict(score=min(score,category,icon),position=position,template='header.png',
                        method='smoothed_parts',scale=scale,full_score=score,
                        category_score=category,icon_score=icon,
                        valid=score>=.93 and category>=.88 and icon>=.90)

        best=match(1.)
        if best is not None and best['valid']:return best
        candidates=[best] if best is not None else []
        for scale in np.arange(.65,1.351,.05):
            if abs(scale-1.)<.001:continue
            result=match(float(round(scale,2)))
            if result is not None:candidates.append(result)
        if not candidates:
            return dict(score=0.,position=(0,0),template='header.png',scale=1.,valid=False)
        best=max(candidates,key=lambda r:r['full_score'])
        for offset in (-.02,-.01,.01,.02):
            result=match(round(best['scale']+offset,2))
            if result is not None:candidates.append(result)
        return max(candidates,key=lambda r:(r['valid'],r['full_score']))

    def locate_grid(self,f):
        """Locate repeated opaque quantity bars, independently of header alignment.

        Only bars with a readable x-prefixed quantity vote for the grid. Scenery
        and lock/favorite badges therefore cannot establish an inventory layout.
        """
        # Three elementwise passes avoid the expensive per-pixel axis reduction.
        blue,green,red=cv2.split(f)
        maximum=cv2.max(cv2.max(blue,green),red)
        minimum=cv2.min(cv2.min(blue,green),red)
        mask=((maximum<65)&(minimum>15)&((maximum.astype(np.int16)-minimum)<17)).astype(np.uint8)*255
        mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((3,5),np.uint8))
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        anchors=[]
        for contour in contours:
            x,y,w,h=cv2.boundingRect(contour)
            if not (112<=w<=140 and 22<=h<=42 and x>=40 and y>=180):continue
            left=round(x+(w-135)/2);top=y+h-135
            if top<80 or left<0 or left+135>f.shape[1] or top+137>f.shape[0]:continue
            amount,_=self.reader.read(f[top+103:top+137,left:left+135])
            if amount is not None:anchors.append((left,top))
        if not anchors:return None
        pitch=148.2
        def residual(delta):return abs(delta-round(delta/pitch)*pitch)
        groups=[[p for p in anchors if residual(p[0]-a[0])<=5 and residual(p[1]-a[1])<=5] for a in anchors]
        group=max(groups,key=len)
        # A lone unrelated number must not relocate a complete bag.
        if len(group)<2:return None
        # Measure spacing instead of accumulating a fixed 148.2-pixel pitch
        # across every column. Native rounding and UI scaling affect both axes.
        spacings=[]
        for i,(ax,ay) in enumerate(group):
            for bx,by in group[i+1:]:
                for delta in (abs(bx-ax),abs(by-ay)):
                    steps=round(delta/pitch)
                    if steps:spacings.append(delta/steps)
        pitch=float(np.median(spacings))
        ax,ay=group[0]
        x=float(np.median([px-round((px-ax)/pitch)*pitch for px,py in group]))
        y=float(np.median([py-round((py-ay)/pitch)*pitch for px,py in group]))
        x+=min(round((px-x)/pitch) for px,py in group)*pitch
        y+=min(round((py-y)/pitch) for px,py in group)*pitch
        # Recover a selected/connected first tile if its dark bar was not a contour.
        # Check adjacent columns/rows using the existing quantity and tile checks.
        def occupied(px,py):
            if self.tile_present(f,px,py):return True
            if px<0 or py<0 or px+135>f.shape[1] or py+137>f.shape[0]:return False
            # Selected cards can have a colored translucent fill. A readable
            # quantity and a known icon independently prove their presence.
            amount,_=self.reader.read(f[py+103:py+137,px:px+135])
            if amount is None:return False
            key,_=self.identify(f[py+17:py+103,px+19:px+117],core=f[py+26:py+101,px+30:px+105])
            return key is not None
        while x-pitch>=40 and any(occupied(round(x-pitch),round(y+r*pitch)) for r in range(5)):
            x-=pitch
        while y-pitch>=80 and any(self.tile_present(f,round(x+c*pitch),round(y-pitch)) for c in range(12)):
            y-=pitch
        columns=max(round((px-x)/pitch) for px,py in group)+1
        return x,y,len(group),columns,pitch

    def tile_present(self,f,x,y,partial=False):
        required_width=28 if partial else 135
        if x<0 or y<0 or x+required_width>f.shape[1] or y+137>f.shape[0]:return False
        strip=f[y+109:y+128,x+13:x+126]
        hsv=cv2.cvtColor(strip,cv2.COLOR_BGR2HSV)
        dark=np.mean((hsv[:,:,1]<60)&(hsv[:,:,2]<80))
        cream=f[y+19:y+28,x+7:x+14].mean(axis=(0,1))
        lower_left=f[y+75:y+95,x+3:x+7].mean(axis=(0,1))
        def card_fill(color):
            return min(color)>130 or (color[2]>150 and color[1]>70 and color[0]<100)
        # A bright character in the dark details panel is not another ball
        # column. Require opaque card fill on both sides of the icon as well.
        left_fill=card_fill(cream) or card_fill(lower_left)
        if partial:return bool(dark>.48 and left_fill)
        right=f[y+35:y+48,x+121:x+127].mean(axis=(0,1))
        return bool(dark>.48 and card_fill(right) and left_fill)

    def detect(self, frame, full_height=None, probe=False):
        full_height=full_height or frame.shape[0]
        f=self.normalize(frame,full_height)
        if f.shape[0]<900 or f.shape[1]<900:return BagRead(False,False,[],reason='游戏画面不完整，暂不更新背包')
        header=self.match_header(f)
        scale=header['scale']
        if header['valid'] and scale!=1.:
            # Resample the original capture once, not the already resized image.
            f=self.normalize(frame,full_height,scale)
        header_score,loc,header_template=header['score'],header['position'],header['template']
        loc=tuple(round(value/scale) for value in loc)
        if not header['valid']:
            grid=self.locate_grid(f) if probe else None
            if grid and grid[2]>=3:
                return BagRead(False,False,[],reason='检测到球列表，但背包标题匹配不足，暂不更新',header_score=header_score,
                               layout=dict(left=grid[0],top=grid[1],anchor_votes=grid[2],columns=grid[3],
                                           header_template=header_template,header_match=header,diagnostic_only=True))
            return BagRead(False,False,[],reason='等待打开咕噜球背包',header_score=header_score)
        detected=self.locate_grid(f)
        # Subpixel pitch changes within one pixel are contour rounding noise.
        if detected and detected[2]>=3 and abs(detected[4]-148.2)>1.:
            # The small title is a coarse scale estimate. Repeated card spacing
            # spans the whole grid and supplies a more precise measurement.
            scale*=detected[4]/148.2
            f=self.normalize(frame,full_height,scale)
            loc=tuple(round(value/scale) for value in header['position'])
            detected=self.locate_grid(f)
        layout={'header_template':header_template,'header_match':header,'ui_scale':scale}
        def reading(valid,balls,compact=False,reason=''):
            return BagRead(True,valid,balls,compact,reason,header_score,layout)
        dx,dy=loc[0]-98,loc[1]-14
        left,top,votes,found_columns,pitch=detected if detected else (247+dx,134+dy,0,1,148.2)
        capacity=max(0,int((f.shape[1]-left-135)//pitch)+1)
        columns=min(capacity,found_columns)
        for col in range(columns,capacity):
            if any(self.tile_present(f,round(left+col*pitch),round(top+r*pitch)) for r in range(5)):
                columns=col+1
            else:break  # A compact inventory cannot resume beyond an empty column.
        layout.update(left=round(left,2),top=round(top,2),pitch=round(pitch,2),scanned_columns=columns,anchor_votes=votes,
                      header_x=loc[0],header_y=loc[1])
        # Inspect the left edge of the first incomplete column: a clipped card
        # must not disappear merely because its quantity cannot be read.
        boundary=round(left+capacity*pitch)
        clipped=columns>=capacity and (boundary+28>f.shape[1] or any(
            self.tile_present(f,boundary,round(top+row*pitch),partial=True) for row in range(5)))
        if columns<1 or clipped or top<80 or top+4*pitch+137>f.shape[0]:
            return reading(False,[],reason='背包范围未完整显示，暂不更新')
        balls=[]; occupied=[]
        for row in range(5):
            for col in range(columns):
                slot=row*columns+col;x=round(left+col*pitch);y=round(top+row*pitch)
                # Tile corners and opaque number strip jointly prove that a card exists.
                tile=self.tile_present(f,x,y)
                patch=f[y+103:y+137,x:x+135]
                amount,quality=self.reader.read(patch)
                if not tile and amount is None:continue
                occupied.append((row,col))
                if amount is None:return reading(False,balls,reason=f'第 {row+1} 行第 {col+1} 格数字未看清，保留上次数据')
                icon=f[y+26:y+101,x+30:x+105].copy()
                key,icon_quality=self.identify(f[y+17:y+103,x+19:x+117],core=icon)
                balls.append(BallRead(key,amount,icon,slot,quality,icon_quality))
        if not balls:return reading(False,[],reason='背包为空或布局未识别，保留上次数据')
        known=[b.key for b in balls if b.key]
        if len(set(known))!=len(known):return reading(False,balls,reason='图标匹配有歧义，等待稳定画面')
        actual_columns=max(col for row,col in occupied)+1
        aligned=[row*actual_columns+col for row,col in occupied]==list(range(len(occupied)))
        if not aligned:return reading(False,balls,reason='列表未完整对齐，保留上次数据')
        for ball,(row,col) in zip(balls,occupied):ball.slot=row*actual_columns+col
        layout['columns']=actual_columns
        return reading(True,balls,compact=len(occupied)<5*actual_columns)

    def register(self, ball):
        if ball.key:return ball.key
        key='custom_'+hashlib.sha256(ball.icon.tobytes()).hexdigest()[:12]
        self.catalog[key]=ball.icon.copy()
        self.icon_cache.clear()
        self.icon_features.pop(key,None)
        if self.custom_dir:
            self.custom_dir.mkdir(parents=True,exist_ok=True)
            ok,encoded=cv2.imencode('.png',ball.icon)
            if not ok:raise OSError('无法保存新球图标')
            encoded.tofile(str(self.custom_dir/f'{key}.png'))
        ball.key=key
        return key


class BagStabilizer:
    def __init__(self):self.reset()
    def reset(self):self.previous=None;self.frames=0;self.since=None
    def push(self, result, now):
        if not result.valid:self.reset();return None
        same=self.previous is not None and len(result.balls)==len(self.previous.balls)
        if same:
            for a,b in zip(result.balls,self.previous.balls):
                if a.amount!=b.amount or a.key!=b.key or np.mean(np.abs(a.icon.astype(float)-b.icon.astype(float)))>14:
                    same=False;break
        if not same:self.frames=1;self.since=now
        else:self.frames+=1
        self.previous=result
        return result if self.frames>=3 and now-self.since>=.7 else None


class InventorySession:
    """Only snapshot-to-snapshot changes are knowable from images."""
    def __init__(self):
        self.rows={};self.initialized=False;self.last_signature=None

    def apply(self, amounts, allow_missing_zero=False):
        values=dict(amounts)
        # The recognizer must establish a complete, compact list before absence
        # can mean zero. An uncertain capture never reaches this method.
        if allow_missing_zero:
            for key in self.rows:values.setdefault(key,0)
        signature=tuple(sorted(values.items()))
        if signature==self.last_signature:return False
        first=not self.initialized
        for key,amount in values.items():
            if key not in self.rows:
                baseline=amount if first or not allow_missing_zero else 0
                self.rows[key]={'initial':baseline,'current':amount,'used':0,'added':max(0,amount-baseline),'seen':True}
            else:
                row=self.rows[key];delta=amount-row['current']
                row['used']+=max(0,-delta);row['added']+=max(0,delta);row['current']=amount
        self.last_signature=signature;self.initialized=True
        return True
