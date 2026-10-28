"""Relative view rotation from shared features, never proof of traversable floor.

Fixed servo and an approximately stationary rotation centre are required. Angles
use the existing approximate lens calibration, not an external compass. Weak
overlap, parallax or incoherent motion returns unknown rather than a turn.
"""
import math

import cv2
import numpy as np

try:
    from .camera_radar import FOCAL_PX
except ImportError:
    from camera_radar import FOCAL_PX


def _rotation(a, b):
    u, _, vt = np.linalg.svd(a.T @ b)
    r = vt.T @ u.T
    if np.linalg.det(r) < 0:
        vt[-1] *= -1
        r = vt.T @ u.T
    return r


def estimate_heading(before, after):
    """Signed before -> after yaw (+ left), plus independently measured quality.

No command, expected angle, target heading or AI correction enters this fit.
"""
    result = dict(reliable=False, yaw_deg=None, correction_deg=None, reason='invalid_image')
    try:
        images = [cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE) for raw in (before, after)]
        if any(img is None or min(img.shape) < 120 for img in images):
            return result
        # Camera profiles preserve the field of view. Low resolution is retained
        # in the diagnostics; resizing does not create new real landmarks.
        result['sizes'] = [[int(i.shape[1]), int(i.shape[0])] for i in images]
        images = [cv2.resize(i, (640, 480)) for i in images]
        sift = cv2.SIFT_create(nfeatures=1200, contrastThreshold=.015)
        features = [sift.detectAndCompute(i, None) for i in images]
        if any(d is None for _, d in features):
            return dict(result, reason='no_features')
        (ka, da), (kb, db) = features
        matcher = cv2.BFMatcher()
        ab = {m.queryIdx: m.trainIdx for pair in matcher.knnMatch(da, db, k=2)
              if len(pair) == 2 for m, n in [pair] if m.distance < .72*n.distance}
        ba = {m.queryIdx: m.trainIdx for pair in matcher.knnMatch(db, da, k=2)
              if len(pair) == 2 for m, n in [pair] if m.distance < .72*n.distance}
        pairs = [(a, b) for a, b in ab.items() if ba.get(b) == a]
        result['matches'] = len(pairs)
        if len(pairs) < 18:
            return dict(result, reason='few_shared_features')
        pa = np.array([ka[a].pt for a, _ in pairs])
        pb = np.array([kb[b].pt for _, b in pairs])
        def rays(points):
            r = np.c_[(points-[320, 240])/FOCAL_PX, np.ones(len(points))]
            return r/np.linalg.norm(r, axis=1)[:, None]
        a, b = rays(pa), rays(pb)
        def errors(r):
            return np.degrees(np.arccos(np.clip(np.sum((a @ r.T)*b, axis=1), -1, 1)))
        rng, best = np.random.default_rng(0), np.zeros(len(a), dtype=bool)
        for _ in range(160):
            sample = rng.choice(len(a), 3, replace=False)
            r = _rotation(a[sample], b[sample])
            keep = errors(r) < 2.
            if keep.sum() > best.sum():
                best = keep
        if best.sum() < 14:
            return dict(result, reason='incoherent_rotation', inliers=int(best.sum()))
        for _ in range(3):
            r = _rotation(a[best], b[best])
            keep = errors(r) < 2.
            if keep.sum() < 14:
                return dict(result, reason='unstable_fit')
            best = keep
        r = _rotation(a[best], b[best])
        err = errors(r)[best]
        spans = [np.ptp(p[best], axis=0)/[640, 480] for p in (pa, pb)]
        coverage = np.min(spans, axis=0)
        # Extract the camera's relative yaw; other axes diagnose changed tilt
        # or grossly non-rotational correspondence. Same servo is checked by caller.
        yaw = math.degrees(math.atan2(r[0, 2], r[2, 2]))
        pitch = math.degrees(math.asin(np.clip(-r[1, 2], -1, 1)))
        roll = math.degrees(math.atan2(r[1, 0], r[1, 1]))
        result.update(inliers=int(best.sum()), inlier_ratio=round(float(best.mean()), 3),
                      median_error_deg=round(float(np.median(err)), 3),
                      coverage=[round(float(v), 3) for v in coverage],
                      pitch_deg=round(pitch, 2), roll_deg=round(roll, 2))
        if best.mean() < .45 or min(coverage) < .2 or np.median(err) > 1.2:
            return dict(result, reason='weak_rotation_geometry')
        vector = np.degrees(cv2.Rodrigues(r)[0].ravel())
        angle = float(np.linalg.norm(vector))
        # The camera is pitched even at servo 90. Body yaw then appears as
        # camera yaw AND roll. Axis-angle magnitude is invariant to that pitch.
        if (angle > 50 or abs(vector[0]) > max(3, angle*.2)
                or abs(vector[2]) > max(3, angle*.55)):
            return dict(result, reason='outside_rotation_envelope')
        turned = math.copysign(angle, yaw)
        return dict(result, reliable=True, reason='shared_features', optical_yaw_deg=round(yaw, 2),
                    yaw_deg=round(turned, 2), correction_deg=round(-turned, 2))
    except (cv2.error, ValueError, TypeError, np.linalg.LinAlgError):
        return dict(result, reason='invalid_or_degenerate_image')
