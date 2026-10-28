"""Experimental two-image ground-plane motion. No simulator or control imports.

Body frame: forward +x, left +y, positive yaw turns left. The camera must be
level and fixed. Metric scale comes ONLY from the supplied camera height.
No pose, map, world geometry or checkpoint is accepted as an input. An optional
accepted command only seeds feature search; it never supplies the measurement.
OpenCV/numpy are optional; unavailable dependencies produce an unknown result.
"""
from dataclasses import dataclass
import math

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = np = None


@dataclass(frozen=True)
class Calibration:
    width: int = 640
    height: int = 480
    focal_px: float = 280.
    camera_height_m: float = .22
    center_x: float = 320.
    horizon_y: float = 240.
    min_floor_y: float = 285.
    max_translation_m: float = .60
    max_yaw_rad: float = 1.


def _unknown(reason, **diagnostics):
    return dict(status='unknown', motion=None, uncertainty=None, reason=reason,
                method='floor_lk_ransac_se2', diagnostics=diagnostics)


def _ground(points, calibration):
    depth = calibration.camera_height_m * calibration.focal_px / (points[:, 1] - calibration.horizon_y)
    left = (calibration.center_x - points[:, 0]) * depth / calibration.focal_px
    return np.column_stack((depth, left))


def _rigid(after, before):
    """Map static landmarks from AFTER body coordinates to BEFORE coordinates."""
    ca, cb = after.mean(axis=0), before.mean(axis=0)
    u, _, vt = np.linalg.svd((after-ca).T @ (before-cb))
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1] *= -1
        rotation = vt.T @ u.T
    translation = cb - rotation @ ca
    return rotation, translation


def _estimate_seed(before_jpeg, after_jpeg, calibration=None, *, before_servo=90, after_servo=90,
                   seed=(0., 0., 0.)):
    """Estimate bounded planar camera motion, or return motion=None.

    This sparse local tracker needs overlap and texture. Repetitive patterns,
    insufficient floor, changed tilt and large displacements are not resolved by
    command integration. A valid estimate is still a fallible visual estimate.
    """
    if cv2 is None or np is None:
        return _unknown('opencv_numpy_unavailable')
    calibration = calibration or Calibration()
    if (not isinstance(calibration, Calibration) or before_servo != 90 or after_servo != 90 or
            not all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (
                calibration.width, calibration.height, calibration.focal_px,
                calibration.camera_height_m, calibration.max_translation_m, calibration.max_yaw_rad)) or
            not calibration.horizon_y < calibration.min_floor_y < calibration.height-10):
        return _unknown('unsupported_calibration_or_tilt')
    if not isinstance(before_jpeg, bytes) or not isinstance(after_jpeg, bytes):
        return _unknown('invalid_image')
    if before_jpeg == after_jpeg:
        return _unknown('identical_images_do_not_establish_metric_motion')
    try:
        images = [cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
                  for raw in (before_jpeg, after_jpeg)]
        if any(im is None or im.shape != (calibration.height, calibration.width) for im in images):
            return _unknown('invalid_image_or_dimensions')
        before, after = images
        mask = np.zeros_like(before)
        mask[int(calibration.min_floor_y):calibration.height-12, 12:calibration.width-12] = 255
        corners = cv2.goodFeaturesToTrack(before, 350, .025, 9, mask=mask, blockSize=7)
        if corners is None or len(corners) < 8:
            return _unknown('insufficient_floor_features', features=0 if corners is None else len(corners))
        parameters = dict(winSize=(25, 25), maxLevel=3,
                          criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 35, .005))
        initial = None
        if seed != (0., 0., 0.):
            points = _ground(corners.reshape(-1, 2), calibration)
            angle = seed[2]
            rotation = np.array([[math.cos(angle), -math.sin(angle)],
                                 [math.sin(angle), math.cos(angle)]])
            projected = (points-np.array(seed[:2])) @ rotation
            with np.errstate(divide='ignore', invalid='ignore'):
                initial = np.column_stack((calibration.center_x-calibration.focal_px*projected[:, 1]/projected[:, 0],
                    calibration.horizon_y+calibration.camera_height_m*calibration.focal_px/projected[:, 0]))
            valid = ((projected[:, 0] > .08) & np.isfinite(initial).all(axis=1) &
                     (initial[:, 0] > 10) & (initial[:, 0] < calibration.width-10) &
                     (initial[:, 1] > calibration.min_floor_y) & (initial[:, 1] < calibration.height-10))
            corners = corners[valid]
            initial = initial[valid].astype(np.float32).reshape(-1, 1, 2)
            if len(corners) < 8:
                return _unknown('insufficient_seed_overlap', features=len(corners))
        tracked, status, _ = cv2.calcOpticalFlowPyrLK(before, after, corners, initial,
            flags=cv2.OPTFLOW_USE_INITIAL_FLOW if initial is not None else 0, **parameters)
        back, reverse_status, _ = cv2.calcOpticalFlowPyrLK(after, before, tracked, corners.copy(),
            flags=cv2.OPTFLOW_USE_INITIAL_FLOW, **parameters)
        p, q, r = corners.reshape(-1, 2), tracked.reshape(-1, 2), back.reshape(-1, 2)
        good = ((status.ravel() == 1) & (reverse_status.ravel() == 1) &
                (np.linalg.norm(p-r, axis=1) < .6) & np.isfinite(q).all(axis=1) &
                (q[:, 1] > calibration.min_floor_y) & (q[:, 1] < calibration.height-10) &
                (q[:, 0] > 10) & (q[:, 0] < calibration.width-10))
        p, q = p[good], q[good]
        diagnostics = dict(features=len(corners), tracked=len(p))
        if len(p) < 8:
            return _unknown('insufficient_bidirectional_tracks', **diagnostics)
        # Fit the metric ground model before selecting a pixel homography: a
        # nearby wall can otherwise win the unconstrained largest-plane vote.
        a, b = _ground(q, calibration), _ground(p, calibration)
        affine, inliers = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC,
            ransacReprojThreshold=.015, maxIters=2000, confidence=.995, refineIters=10)
        if affine is None or inliers is None:
            return _unknown('no_metric_plane_consensus', **diagnostics)
        keep = inliers.ravel().astype(bool)
        diagnostics.update(inliers=int(keep.sum()), inlier_ratio=float(keep.sum()/len(p)))
        if keep.sum() < 6 or keep.mean() < .35:
            return _unknown('weak_metric_plane_consensus', **diagnostics)
        scale = float(math.hypot(affine[0, 0], affine[1, 0]))
        if abs(scale-1) > .025:
            return _unknown('nonrigid_ground_projection', scale=scale, **diagnostics)
        a, b, p, q = a[keep], b[keep], p[keep], q[keep]
        _, homography_mask = cv2.findHomography(p, q, cv2.RANSAC, 1.8, maxIters=1500, confidence=.995)
        if homography_mask is None or homography_mask.mean() < .8:
            return _unknown('no_homography_consensus', **diagnostics)
        rotation, translation = _rigid(a, b)
        residuals = np.linalg.norm((rotation @ a.T).T+translation-b, axis=1)
        residual = float(np.sqrt(np.mean(residuals**2)))
        spread = np.linalg.eigvalsh(np.cov(b.T))
        diagnostics.update(residual_m=residual, scale=scale,
                           image_span_px=[float(np.ptp(p[:, axis])) for axis in (0, 1)])
        if (float(spread[0]) < .001 or np.ptp(p[:, 0]) < 100 or np.ptp(p[:, 1]) < 45 or
                residual > .012):
            return _unknown('insufficient_floor_support', **diagnostics)
        yaw = math.atan2(rotation[1, 0], rotation[0, 0])
        travel = float(np.linalg.norm(translation))
        if travel > calibration.max_translation_m or abs(yaw) > calibration.max_yaw_rad:
            return _unknown('outside_local_tracking_range', **diagnostics)
        # Explicit floors include corner quantization and camera-calibration
        # uncertainty; these are quality bounds, not calibrated probabilities.
        position_error = max(.012, residual*3)
        angle_error = max(.025, residual*3/max(.1, math.sqrt(float(spread[1]))))
        return dict(status='estimated', motion=dict(forward_m=float(translation[0]),
            left_m=float(translation[1]), yaw_rad=yaw),
            uncertainty=dict(translation_m=position_error, yaw_rad=angle_error,
                             interpretation='quality_bounds_not_probability'),
            reason='supported_floor_plane', method='floor_lk_ransac_se2', diagnostics=diagnostics)
    except (cv2.error, ValueError, np.linalg.LinAlgError):
        return _unknown('tracking_or_geometry_failed')


def _search_seeds(command):
    seeds = [(0., 0., 0.)]
    if not isinstance(command, dict) or command.get('acceptance') not in ('accepted', 'transport_accepted'):
        return seeds
    speed, duration = command.get('speed'), command.get('duration_ms')
    if not all(type(value) in (int, float) and math.isfinite(value) for value in (speed, duration)):
        return seeds
    if not 0 <= speed <= 180 or not 0 <= duration <= 2000:
        return seeds
    # These approximate motor calibrations only locate LK search windows. Every
    # proposal, including zero, still needs independently tracked image features.
    amount = max(0., (speed-45)/135)*duration/1000
    action = command.get('action')
    if action in ('rotate_left', 'rotate_right'):
        nominal = min(1., amount*1.2)*(1 if action == 'rotate_left' else -1)
        seeds += [(0., 0., nominal*fraction) for fraction in (.4, .7, 1.)]
    elif action in ('forward', 'backward', 'left', 'right'):
        axis = {'forward': (1, 0), 'backward': (-1, 0), 'left': (0, 1), 'right': (0, -1)}[action]
        nominal = min(.6, amount*.35)
        seeds += [(axis[0]*nominal*fraction, axis[1]*nominal*fraction, 0.) for fraction in (.4, .7, 1.)]
    return seeds


def estimate_motion(before_jpeg, after_jpeg, calibration=None, *, before_servo=90, after_servo=90,
                    command=None):
    """Image-derived SE(2), with optional accepted-command search initialization.

    A prior does not add inliers, relax validation, fabricate partial movement or
    replace failure. Comparable but inconsistent image solutions cause abstention.
    """
    trials = [_estimate_seed(before_jpeg, after_jpeg, calibration,
              before_servo=before_servo, after_servo=after_servo, seed=seed)
              for seed in _search_seeds(command)]
    usable = [trial for trial in trials if trial['motion'] is not None]
    if not usable:
        result = trials[0]
        result['diagnostics']['search_attempts'] = len(trials)
        return result
    usable.sort(key=lambda item: item['diagnostics']['inliers'], reverse=True)
    best = usable[0]
    for candidate in usable[1:]:
        if candidate['diagnostics']['inliers'] < best['diagnostics']['inliers']*.85:
            continue
        a, b = candidate['motion'], best['motion']
        if (math.hypot(a['forward_m']-b['forward_m'], a['left_m']-b['left_m']) > .035 or
                abs(a['yaw_rad']-b['yaw_rad']) > .05):
            return _unknown('ambiguous_image_solutions', search_attempts=len(trials), solutions=len(usable))
    best['diagnostics'].update(search_attempts=len(trials), image_solutions=len(usable))
    return best
