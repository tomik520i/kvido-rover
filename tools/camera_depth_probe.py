"""Offline camera "pseudo-sonar" probe on saved frames of the real car.

Runs Depth Anything V2 Small (ONNX, relative inverse depth) on stored JPEGs and
turns a band around the horizon into a per-angle closeness scan. The centre of
that scan is compared with the ultrasonic reading saved with the same frame.
No device, server, model API or network access; frames never leave the disk.

The model output is affine-invariant inverse depth: larger means closer, and
scale/shift can differ between images. Metres here come only from a global
fit against sonar and are an uncertain estimate, not a measurement.
"""
import argparse
import html
import json
import math
from pathlib import Path
import sys
import time

import cv2
import numpy as np
import onnxruntime

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT/'data/models/depth-anything-v2-small/model.onnx'
FRAMES = ROOT/'reports/private'
MEAN = np.array([.485, .456, .406], np.float32)
STD = np.array([.229, .224, .225], np.float32)


def load_frames(root):
    """(jpeg, sonar_cm|None, servo) for every saved frame with known telemetry."""
    frames = []
    for observer in sorted(root.glob('*/observer.jsonl')):
        seen = {}
        for line in observer.read_text(encoding='utf-8').splitlines():
            result = ((json.loads(line).get('task') or {}).get('last_result')) or {}
            if isinstance(result.get('step'), int):
                seen[result['step']] = result
        for step, result in seen.items():
            for phase in ('before', 'after'):
                jpeg = observer.parent/f'step-{step}-{phase}.jpg'
                observed = result.get('observed_'+phase) or {}
                if jpeg.exists():
                    frames.append(dict(jpeg=jpeg, sonar_cm=observed.get('distance_cm'), servo=observed.get('servo', 90)))
    # Supervised calibration runs (tools/calibration_drive.py): median of standing samples.
    for timeline in sorted(root.glob('calibration-*/timeline.jsonl')):
        for line in timeline.read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            jpeg = timeline.parent/f"{row.get('label')}.jpg"
            if row.get('event') == 'snap' and jpeg.exists():
                frames.append(dict(jpeg=jpeg, sonar_cm=row.get('median'), servo=row.get('servo') or 90))
    for meta in sorted(root.glob('*.json')):
        jpeg = meta.with_suffix('.jpg')
        if not jpeg.exists():
            continue
        telemetry = json.loads(meta.read_text(encoding='utf-8')).get('telemetry') or {}
        frames.append(dict(jpeg=jpeg, sonar_cm=telemetry.get('distance_cm'), servo=telemetry.get('servo', 90)))
    return frames


def imread(path):
    # cv2.imread cannot open non-ASCII Windows paths such as 'autíčko'.
    return cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)


def imwrite(path, img):
    cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 82])[1].tofile(str(path))


def infer(session, bgr, height=518):
    h, w = bgr.shape[:2]
    width = max(14, round(height*w/h/14)*14)
    rgb = cv2.cvtColor(cv2.resize(bgr, (width, height), interpolation=cv2.INTER_CUBIC), cv2.COLOR_BGR2RGB)
    x = ((rgb.astype(np.float32)/255-MEAN)/STD).transpose(2, 0, 1)[None]
    out = session.run(None, {'pixel_values': x})[0][0]
    return cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR)


def scan(disparity, servo, hfov_deg, bins, band_deg):
    """Closeness per horizontal angle from rows within band_deg of the horizon."""
    h, w = disparity.shape
    focal = w/2/math.tan(math.radians(hfov_deg)/2)
    # Same tilt model as the simulator camera: servo 90 looks level.
    horizon = h/2-math.tan(math.radians((90-servo)*.55))*focal
    half = math.tan(math.radians(band_deg))*focal
    top, bottom = int(max(0, horizon-half)), int(min(h, horizon+half))
    if bottom-top < 4:
        return None, (top, bottom)
    band = disparity[top:bottom]
    edges = np.linspace(0, w, bins+1).astype(int)
    closeness = [float(np.percentile(band[:, a:b], 90)) for a, b in zip(edges, edges[1:])]
    angles = [math.degrees(math.atan((w/2-(a+b)/2)/focal)) for a, b in zip(edges, edges[1:])]
    return dict(angles_deg=angles, closeness=closeness), (top, bottom)


def centre_closeness(disparity, rows, cone_deg, hfov_deg):
    h, w = disparity.shape
    focal = w/2/math.tan(math.radians(hfov_deg)/2)
    half = int(math.tan(math.radians(cone_deg))*focal)
    return float(np.percentile(disparity[rows[0]:rows[1], w//2-half:w//2+half], 90))


def spearman(a, b):
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def colour(disparity):
    lo, hi = np.percentile(disparity, 2), np.percentile(disparity, 98)
    norm = np.clip((disparity-lo)/max(1e-6, hi-lo), 0, 1)
    return cv2.applyColorMap((norm*255).astype(np.uint8), cv2.COLORMAP_INFERNO)


def polar(result, sonar_cm, fit, size=300):
    img = np.full((size, size, 3), 24, np.uint8)
    cx, cy, radius = size//2, size-12, size-30
    for r in (.5, 1, 2):
        rr = int(radius*r/2.5)
        cv2.ellipse(img, (cx, cy), (rr, rr), 0, 180, 360, (70, 70, 70), 1)
        cv2.putText(img, f'{r:g} m', (cx+rr-26, cy-4), cv2.FONT_HERSHEY_SIMPLEX, .35, (130, 130, 130), 1)
    points = []
    lo, hi = min(result['closeness']), max(result['closeness'])
    for angle, close in zip(result['angles_deg'], result['closeness']):
        # Without a valid scale, draw per-image rank only: nearest bin inner, farthest outer.
        metres = metres_from(close, fit) if fit else .3+2*(hi-close)/max(1e-6, hi-lo)
        if metres is None:
            continue
        rr = radius*min(metres, 2.5)/2.5
        a = math.radians(90+angle)
        points.append((int(cx+rr*math.cos(a)), int(cy-rr*math.sin(a))))
    for p, q in zip(points, points[1:]):
        cv2.line(img, p, q, (80, 200, 255), 2)
    for p in points:
        cv2.circle(img, p, 3, (80, 200, 255), -1)
    if sonar_cm is not None:
        rr = int(radius*min(sonar_cm/100, 2.5)/2.5)
        cv2.circle(img, (cx, cy-rr), 6, (80, 255, 120), 2)
    cv2.putText(img, 'kamera' if fit else 'kamera: jen poradi blize/dale, bez metru', (8, 16), cv2.FONT_HERSHEY_SIMPLEX, .45, (80, 200, 255), 1)
    cv2.putText(img, 'ultrazvuk', (8, 34), cv2.FONT_HERSHEY_SIMPLEX, .45, (80, 255, 120), 1)
    return img


def metres_from(close, fit):
    if not fit:
        return None
    inverse = fit['a']*close+fit['b']
    return 1/inverse if inverse > 1e-3 else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--frames', type=Path, default=FRAMES)
    parser.add_argument('--out', type=Path, default=FRAMES/'camera-depth-probe')
    parser.add_argument('--hfov', type=float, default=60., help='Assumed horizontal field of view; not calibrated.')
    parser.add_argument('--bins', type=int, default=24)
    parser.add_argument('--band', type=float, default=6., help='Half-height of the horizon band in degrees.')
    parser.add_argument('--fit-max-cm', type=int, default=60, help='Use only sonar readings up to this range for the scale fit.')
    args = parser.parse_args()
    session = onnxruntime.InferenceSession(str(MODEL), providers=['CPUExecutionProvider'])
    args.out.mkdir(parents=True, exist_ok=True)
    frames = load_frames(args.frames)
    rows, started = [], time.monotonic()
    for i, frame in enumerate(frames, 1):
        bgr = imread(str(frame['jpeg']))
        t = time.monotonic()
        disparity = infer(session, bgr)
        seconds = time.monotonic()-t
        result, band = scan(disparity, frame['servo'], args.hfov, args.bins, args.band)
        centre = centre_closeness(disparity, band, 7.5, args.hfov) if result else None
        name = f"{frame['jpeg'].parent.name}--{frame['jpeg'].stem}" if frame['jpeg'].parent != args.frames else frame['jpeg'].stem
        rows.append(dict(name=name, jpeg=str(frame['jpeg'].relative_to(ROOT)), sonar_cm=frame['sonar_cm'], servo=frame['servo'],
                         band_rows=band, centre_closeness=centre, scan=result, inference_s=round(seconds, 3),
                         brightness=float(bgr.mean())))
        np.save(args.out/f'{name}.npy', disparity.astype(np.float16))
        print(f'{i}/{len(frames)} {name} sonar={frame["sonar_cm"]} centre={centre} {seconds:.2f}s', flush=True)
    # Global affine fit of inverse depth: 1/metres = a*closeness + b, near readings only.
    pairs = [(r['centre_closeness'], r['sonar_cm']) for r in rows if r['centre_closeness'] is not None and r['sonar_cm']]
    near = [(c, s) for c, s in pairs if s <= args.fit_max_cm]
    fit = rejected_fit = None
    if len(near) >= 3 and len({s for _, s in near}) >= 2:
        a, b = np.polyfit([c for c, _ in near], [100/s for _, s in near], 1)
        # Closer must mean larger closeness; otherwise the global fit is meaningless.
        fit = dict(a=float(a), b=float(b), pairs=len(near)) if a > 0 else None
        rejected_fit = None if fit else dict(a=float(a), b=float(b), pairs=len(near), reason='non_positive_slope')
    summary = dict(experimental=True, offline=True, model='onnx-community/depth-anything-v2-small (Apache-2.0)',
                   assumed_hfov_deg=args.hfov, frames=len(rows), frames_with_sonar=len(pairs),
                   sonar_near_pairs=len(near), fit=fit, rejected_fit=rejected_fit,
                   spearman_closeness_vs_inverse_sonar=spearman([c for c, _ in pairs], [1/s for _, s in pairs]) if len(pairs) > 2 else None,
                   spearman_near_only=spearman([c for c, _ in near], [1/s for _, s in near]) if len(near) > 2 else None,
                   mean_inference_s=round(float(np.mean([r['inference_s'] for r in rows])), 3) if rows else None,
                   seconds=round(time.monotonic()-started, 1))
    for r in rows:
        r['centre_estimate_cm'] = round(100*metres_from(r['centre_closeness'], fit)) if fit and r['centre_closeness'] and metres_from(r['centre_closeness'], fit) else None
        bgr = imread(str(ROOT/r['jpeg']))
        depth = colour(np.load(args.out/f"{r['name']}.npy").astype(np.float32))
        for img in (bgr, depth):
            cv2.rectangle(img, (0, r['band_rows'][0]), (img.shape[1]-1, r['band_rows'][1]), (255, 255, 255), 1)
        panel = polar(r['scan'], r['sonar_cm'], fit, 480) if r['scan'] else np.zeros((480, 480, 3), np.uint8)
        imwrite(args.out/f"{r['name']}.jpg", np.hstack([bgr, depth, panel]))
    (args.out/'summary.json').write_text(json.dumps(dict(summary=summary, rows=rows), ensure_ascii=False, indent=1), encoding='utf-8')
    cards = ''.join(
        f"<figure><img src='{html.escape(r['name'])}.jpg' loading=lazy><figcaption>{html.escape(r['name'])} · "
        f"ultrazvuk {r['sonar_cm'] if r['sonar_cm'] is not None else 'null'} cm · kamera "
        f"{r['centre_estimate_cm'] if r['centre_estimate_cm'] is not None else '?'} cm · servo {r['servo']}</figcaption></figure>"
        for r in rows)
    (args.out/'index.html').write_text(
        "<!doctype html><meta charset=utf-8><title>Kamerový pseudo-sonar</title><style>body{font:14px system-ui;background:#111;color:#ddd;margin:16px}"
        "img{width:100%;max-width:1760px}figure{margin:0 0 24px}pre{background:#222;padding:8px;overflow:auto}</style>"
        "<h1>Kamerový pseudo-sonar (offline, Depth Anything V2 Small)</h1><p>Vlevo snímek, uprostřed relativní hloubka (světlejší = blíž), "
        "vpravo sken z pruhu kolem horizontu (bílý obdélník). Zelený kroužek = uložená hodnota ultrazvuku. Metry jsou jen nejistý odhad z fitu.</p>"
        f"<pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=1))}</pre>{cards}", encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    sys.exit(main())
