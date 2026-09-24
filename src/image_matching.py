"""Shared image loading and numerically stable template matching."""
import cv2
import numpy as np

# Bound OpenCV worker parallelism for the two recognition loops.
cv2.setNumThreads(2)


def read_image(path, flags=cv2.IMREAD_COLOR):
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), flags)
    if image is None:
        raise ValueError(f'无法读取识别模板：{path}')
    return image


def feature(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    # High-pass pixels preserve letter shapes while suppressing scenery/background.
    return np.rint(gray - cv2.GaussianBlur(gray, (0, 0), 2.0))


def feature_integrals(image):
    return (cv2.integral(image,sdepth=cv2.CV_64F),
            cv2.integral(image*image,sdepth=cv2.CV_64F))


def stable_match_peak(image, template, integrals=None, minimum=0.):
    """Validate the winning window first; build a variance map only if needed.

    With minimum=0 this preserves the original masked-map maximum. Callers can
    discard scores below their candidate floor before doing variance work.
    Most real frames have an energetic winner, so checking every pixel is waste.
    """
    result=cv2.matchTemplate(image,template,cv2.TM_CCOEFF_NORMED)
    _,score,_,position=cv2.minMaxLoc(result)
    if np.isfinite(score) and score<minimum:return 0.,(0,0)
    x,y=position;h,w=template.shape[:2]
    integral,squares=integrals if integrals is not None else feature_integrals(image)
    def total(a):return a[y+h,x+w]-a[y,x+w]-a[y+h,x]+a[y,x]
    mean=total(integral)/(h*w)
    if np.isfinite(score) and score>0 and total(squares)/(h*w)-mean*mean>=9:
        return score,position
    # Flat/degenerate regions can produce a spurious maximum. Retain the full
    # original masking semantics without repeatedly searching invalid peaks.
    def sums(a):
        values=cv2.subtract(a[h:,w:],a[:-h,w:])
        cv2.subtract(values,a[h:,:-w],dst=values)
        cv2.add(values,a[:-h,:-w],dst=values)
        return values
    means=sums(integral)
    cv2.multiply(means,1./(h*w),dst=means)
    variance=sums(squares)
    cv2.multiply(variance,1./(h*w),dst=variance)
    cv2.subtract(variance,cv2.multiply(means,means),dst=variance)
    result[(variance<9)|~np.isfinite(result)]=0
    _,score,_,position=cv2.minMaxLoc(result)
    return score,position


def precise_correlation(a,b):
    a=a.astype(np.float64);b=b.astype(np.float64)
    a-=a.mean();b-=b.mean()
    denom=np.linalg.norm(a)*np.linalg.norm(b)
    return float(np.sum(a*b)/denom) if denom>1 else 0.
