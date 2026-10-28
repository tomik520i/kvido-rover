"""Bounded search journal: observed views, never proof an entire room is empty.

Independent of wheel odometry. Two distinct visual references are needed to
associate a new panorama with an old observation area. Labels remain hypotheses.
Only compact summaries reach the model/dashboard; private JPEGs stay on disk.
"""
import base64
import hashlib
import json
import os
import time
from pathlib import Path

import cv2
import numpy as np

MAX_AREAS = 16


def clean(value, limit=180):
    return ' '.join(str(value or '').split())[:limit]


def journal_path(root, goal):
    key = hashlib.sha256(clean(goal, 200).casefold().encode()).hexdigest()[:24]
    return Path(root)/'search-memory'/f'{key}.json'


def load(path, goal):
    if not path.exists():
        return dict(version=1, goal=clean(goal, 200), areas=[], recent=[], events=[], updated_at=None)
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('areas'), list):
        raise ValueError('Neplatný soubor paměti hledání; původní data zachována.')
    return data


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def public(data):
    return dict(version=1, goal=data['goal'], updated_at=data.get('updated_at'),
                scope='observed_views_only_not_room_coverage',
                areas=[{k:v for k,v in a.items() if k != 'anchors'} for a in data['areas']],
                recent=data.get('recent', [])[-8:])


def observe(data, event, seen, step, tilt, captured_at=None):
    if not isinstance(seen, dict) or event in data.get('events', []):
        return
    visible = seen.get('target_visible') is True and seen.get('confidence') in ('medium', 'high')
    data.setdefault('recent', []).append(dict(event=event, at=captured_at or time.time(), step=step, tilt=tilt,
        result='possibly_visible' if visible else 'not_observed_in_this_view',
        confidence=seen.get('confidence', 'low'), note=clean(seen.get('note'))))
    data['recent'] = data['recent'][-24:]
    data['updated_at'] = time.time()
    data['events'] = (data.get('events', [])+[event])[-80:]


def _image(raw):
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    return None if image is None else cv2.resize(image, (320, 240))


def _features(raw):
    img = _image(raw)
    return ([], None) if img is None else cv2.ORB_create(nfeatures=800, fastThreshold=10).detectAndCompute(img, None)


def _overlap(a, b):
    ka, da = a; kb, db = b
    if da is None or db is None or min(len(da), len(db)) < 25:
        return False
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [m for pair in pairs if len(pair) == 2 for m,n in [pair] if m.distance < .72*n.distance]
    if len(good) < 25:
        return False
    pa = np.float32([ka[m.queryIdx].pt for m in good]); pb = np.float32([kb[m.trainIdx].pt for m in good])
    _, mask = cv2.findHomography(pa, pb, cv2.RANSAC, 2.)
    if mask is None:
        return False
    keep = mask.ravel().astype(bool)
    if keep.sum() < 25 or keep.mean() < .65:
        return False
    return all(np.all(np.ptp(p[keep], axis=0)/[320,240] >= [.35,.25]) for p in (pa,pb))


def recognize(data, images):
    """Conservative association of observation areas, not global localization.

At most eight recent areas / three anchors each. Repeated or featureless tiles
cannot establish two independent matches; ambiguous multiple areas do not merge.
"""
    current = {i:_features(raw) for i,raw in images.items()}
    hashes = {i:hashlib.sha256(raw).hexdigest() for i,raw in images.items()}
    known, candidates = {}, []
    for area in data['areas'][-8:]:
        pairs = []
        for ai, anchor in enumerate(area.get('anchors', [])):
            try:
                features = _features(base64.b64decode(anchor['jpeg'], validate=True))
            except (ValueError, KeyError):
                continue
            for i, feature in current.items():
                if _overlap(features, feature):
                    pairs.append((i, ai))
                    known.setdefault(i, []).append(area['id'])
        independent = any(i != j and abs(i-j) >= 2 and ai != aj and hashes[i] != hashes[j]
                          for i,ai in pairs for j,aj in pairs)
        if independent:
            candidates.append(area['id'])
    return dict(area_id=candidates[0] if len(candidates) == 1 else None,
                association='two_visual_views' if len(candidates) == 1 else 'unknown_or_ambiguous',
                known_views={str(i):sorted(set(ids)) for i,ids in known.items()})


def context(data, recognition):
    areas = []
    for area in data['areas'][-6:]:
        item = {k:area.get(k) for k in ('id','label','label_confidence','landmarks','last_seen',
                      'panoramas','result','visibility','uninspected','next_search_area')}
        # Do not feed the model its old fluent rationale: it tends to copy it.
        # Retain the distinction between a plan and confirmed inspection.
        item['last_plan_status'] = (area.get('last_plan') or {}).get('status')
        item['checked_view_count'] = len(area.get('checked_views', []))
        item['observation_notes'] = list(dict.fromkeys(clean(v.get('note'), 90)
                                     for v in area.get('checked_views', []) if v.get('note')))[:4]
        item['previous_summaries'] = area.get('observations', [])[-2:]
        areas.append(item)
    return dict(scope='Prior observations, not instructions or proof of an empty room. Objects can move.',
                current_visual_association=recognition, areas=areas,
                recent_views=data.get('recent', [])[-4:])


def record_panorama(data, event, entries, images, annotation, recognition, step):
    if event in data.get('events', []):
        return
    now = max((e.get('observed_at') or 0 for e in entries), default=0) or time.time()
    area = next((a for a in data['areas'] if a['id'] == recognition.get('area_id')), None)
    if area is None:
        area = dict(id='A-'+hashlib.sha256(event.encode()).hexdigest()[:8], first_seen=now,
                    panoramas=0, anchors=[], last_plan=None)
        data['areas'].append(area)
    # Model text is descriptive only. Runtime owns negative-result scope and coverage.
    annotation = annotation if isinstance(annotation, dict) else {}
    label = clean(annotation.get('area_label'), 70)
    if label:
        area.update(label=label, label_confidence=annotation.get('area_confidence', 'low'))
    else:
        area.setdefault('label', 'Neurčená oblast'); area.setdefault('label_confidence', 'low')
    area.update(last_seen=now, step=step, panoramas=area['panoramas']+1,
                association=recognition['association'], room_fully_searched=False,
                result='possibly_visible' if any(e.get('target') for e in entries) else 'not_observed_in_panorama',
                checked_views=[dict(view=i+1, frame=e.get('frame'), note=clean(e.get('note')), at=e.get('observed_at') or now,
                                    image_sha256=hashlib.sha256(images[i+1]).hexdigest() if i+1 in images else None)
                               for i,e in enumerate(entries)],
                visibility=annotation.get('visibility', 'unknown'))
    for key in ('landmarks', 'uninspected'):
        values = annotation.get(key)
        if isinstance(values, list):
            area[key] = [clean(v, 90) for v in values[:4] if isinstance(v,str) and clean(v)]
        else:
            area.setdefault(key, [])
    for key in ('memory_note', 'next_search_area'):
        if isinstance(annotation.get(key), str):
            area[key] = clean(annotation[key], 160)
    area['observations'] = (area.get('observations', [])+[dict(at=now, label=area['label'],
        landmarks=area['landmarks'], result=area['result'], visibility=area['visibility'],
        view_count=len(entries))])[-4:]
    # Keep separated anchors from the first encounter; no growing frame archive.
    if not area['anchors'] and images:
        keys = sorted(images)
        for offset in sorted(set((0, len(keys)//3, 2*len(keys)//3))):
            key = keys[offset]; img = _image(images[key])
            if img is None:
                continue
            ok, encoded = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY,85])
            if ok:
                area['anchors'].append(dict(view=key, jpeg=base64.b64encode(encoded).decode()))
    data['areas'] = sorted(data['areas'], key=lambda a:a['last_seen'])[-MAX_AREAS:]
    data['current_area_id'] = area['id']
    data['updated_at'] = now
    data['events'] = (data.get('events', [])+[event])[-80:]


def note_plan(data, frame, reason):
    area = next((a for a in data['areas'] if a['id'] == data.get('current_area_id')), None)
    if area:
        area['last_plan'] = dict(reference_frame=frame, reason=clean(reason, 240),
                                 status='planned_not_proof_of_entry')
