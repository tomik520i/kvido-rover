"""Camera "radar": free floor distance per direction from one depth map.

Input is the relative inverse depth from Depth Anything V2 (larger = closer)
for a 640x480 frame of the real car, plus the servo tilt from telemetry.
A flat floor is a straight line of inverse depth against the image row and
the same line in every column; a vertical obstacle keeps a constant inverse
depth. Per direction the scan walks up from the bottom edge and reports the
first row where the column rises above that floor line: the obstacle's
contact with the floor. A per-tilt calibration turns the row into distance.

Calibrations are empirical (one flat cabinet, 33-69 cm, 24 Oct 2026;
docs/KAMERA_HLOUBKA.md). Distances outside that range are extrapolated and
flagged. Never a substitute for the firmware stop; no I/O, no model calls.
"""
import math

import numpy as np

# Row of floor contact y (this module's detector, centre +-8 deg) -> distance
# from the ultrasonic sensor in cm: d = k/(y - c) - o. Fitted by
# fit_calibration on the PWM 180/150 cabinet runs only; the PWM 130 and the
# earlier runs are the held-out test (tools/camera_radar_eval.py --calibrate).
CALIBRATION = {72: dict(c=230.5, k=5574.2, o=12.), 55: dict(c=86.6, k=18333.9, o=79.5), 90: dict(c=235.5, k=22910.5, o=79.5)}
CALIBRATED_CM = (33, 69)
FOCAL_PX = 325.  # from the measured 27 deg per PWM 180/300 ms rotation pulse
WIDTH, HEIGHT = 640, 480
FLOOR_ROWS = (420, 474)


def bearing_deg(column, focal=FOCAL_PX):
    """Positive angles are to the left, like the simulator heading."""
    return math.degrees(math.atan((WIDTH/2-column)/focal))


def row_to_cm(row, tilt):
    cal = CALIBRATION.get(tilt)
    if cal is None or row <= cal['c']+1:
        return None
    return cal['k']/(row-cal['c'])-cal['o']


def fit_calibration(rows, distances_cm):
    """Robust least squares of d = k/(y-c) - o; refits without >3x-median outliers."""
    y, d = np.asarray(rows, float), np.asarray(distances_cm, float)
    keep = np.ones(len(y), bool)
    for _ in range(3):
        best = None
        for o in np.arange(-30, 80, .5):
            A = np.c_[np.ones(keep.sum()), 1/(d[keep]+o)]
            (c, k), *_ = np.linalg.lstsq(A, y[keep], rcond=None)
            error = ((A@[c, k]-y[keep])**2).sum()
            if best is None or error < best[0]:
                best = (error, c, k, o)
        _, c, k, o = best
        residual = np.abs(y-(c+k/(d+o)))
        keep = residual < max(3*np.median(residual[keep]), 3)
    return dict(c=float(c), k=float(k), o=float(o)), keep


def _profiles(disparity, bins):
    edges = np.linspace(0, disparity.shape[1], bins+1).astype(int)
    profiles = np.stack([np.median(disparity[:, a:b], axis=1) for a, b in zip(edges, edges[1:])])
    kernel = np.ones(7)/7
    return edges, np.stack([np.convolve(p, kernel, mode='same') for p in profiles])


def _floor_line(profiles):
    """Robust common floor line from the bottom rows of all directions."""
    rows = np.arange(*FLOOR_ROWS)
    fits = np.array([np.polyfit(rows, p[rows], 1) for p in profiles])
    slope, intercept = np.median(fits[:, 0]), np.median(fits[:, 1])
    # Directions whose bottom rows disagree are blocked right at the bumper.
    residual = np.array([np.median(np.abs(p[rows]-(slope*rows+intercept))) for p in profiles])
    return slope, intercept, residual


def scan(disparity, tilt, bins=32, focal=FOCAL_PX):
    """Return one entry per direction, left to right in the image.

    status: obstacle (distance from floor contact), near (no floor visible
    at the bottom edge), open (floor reaches the top of the search window),
    unknown (no floor line, e.g. the camera sees no floor at all).
    """
    disparity = np.asarray(disparity, dtype=np.float32)
    if disparity.shape != (HEIGHT, WIDTH) or tilt not in CALIBRATION:
        raise ValueError('Expected a 640x480 depth map and a calibrated tilt')
    edges, profiles = _profiles(disparity, bins)
    slope, intercept, residual = _floor_line(profiles)
    spread = float(np.percentile(disparity, 98)-np.percentile(disparity, 2)) or 1.
    out = []
    # Floor must get closer towards the bottom edge; otherwise there is no floor line.
    floor_ok = slope > .002*spread
    top = int(CALIBRATION[tilt]['c'])+4
    tolerance = max(.025*spread, 6*abs(slope))
    for i, profile in enumerate(profiles):
        centre = (edges[i]+edges[i+1])/2
        entry = dict(bearing_deg=round(bearing_deg(centre, focal), 2), row=None, distance_cm=None, status='unknown')
        if floor_ok and residual[i] > tolerance:
            entry['status'] = 'near'
        elif floor_ok:
            above = profile-(slope*np.arange(HEIGHT)+intercept)
            contact = None
            for y in range(FLOOR_ROWS[0], top, -1):
                # Need several consecutive rows above the floor line, not a speckle.
                if all(above[y-j] > tolerance for j in range(6)):
                    contact = y
                    break
            if contact is None:
                entry.update(status='open', row=top, distance_cm=row_to_cm(top, tilt))
            else:
                entry.update(status='obstacle', row=contact, distance_cm=row_to_cm(contact, tilt))
        d = entry['distance_cm']
        entry['extrapolated'] = d is not None and not CALIBRATED_CM[0]-5 <= d <= CALIBRATED_CM[1]+10
        if d is not None:
            entry['distance_cm'] = round(d, 1)
        out.append(entry)
    return dict(tilt=tilt, floor_slope=float(slope), floor_detected=bool(floor_ok), directions=out)


def fuse(scans):
    """Combine scans of the same view (car standing) direction by direction.

    A status needs a majority; an obstacle's distance and row are the median of
    the frames that saw it. Without a majority the nearest obstacle wins (never
    the freer reading), otherwise the direction is unknown. A single-frame false
    contact or a single-frame false gap therefore cannot decide alone.
    """
    if not scans:
        raise ValueError('No scans to fuse')
    if len(scans) == 1:
        return scans[0]
    need = len(scans)//2+1
    fused = []
    for column in zip(*(s['directions'] for s in scans)):
        statuses = [d['status'] for d in column]
        entry = dict(bearing_deg=column[0]['bearing_deg'], row=None, distance_cm=None, status='unknown', extrapolated=False)
        winner = next((s for s in ('near', 'obstacle', 'open') if statuses.count(s) >= need), None)
        pool = [d for d in column if d['status'] == winner] if winner else [d for d in column if d['status'] == 'obstacle']
        if winner == 'near':
            entry['status'] = 'near'
        elif pool:
            distances = sorted(d['distance_cm'] for d in pool if d['distance_cm'] is not None)
            rows = sorted(d['row'] for d in pool if d['row'] is not None)
            middle = distances[len(distances)//2] if winner else (distances[0] if distances else None)
            entry.update(status=winner or 'obstacle', distance_cm=middle,
                         row=rows[len(rows)//2] if rows else None,
                         extrapolated=any(d.get('extrapolated') for d in pool))
        fused.append(entry)
    return dict(tilt=scans[0]['tilt'], floor_slope=float(np.median([s['floor_slope'] for s in scans])),
                floor_detected=sum(s['floor_detected'] for s in scans) >= need, directions=fused, frames=len(scans))
