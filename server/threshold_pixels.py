"""Current-image locator for the SIMULATION'S yellow threshold marker only.

This is neither a real doorway detector nor semantic goal recognition. Callers
must establish target identity separately. No simulator, history, world, model,
network, pose or camera-command input is read. Uses Pillow + standard library.
"""
import io
import math
from statistics import median

from PIL import Image, UnidentifiedImageError


PROVENANCE = 'simulated_colored_threshold_current_pixels'


def _yellow(red, green, blue):
    # Calibrated from actual saved JPEGs: near marker ~232/192/61; a distant
    # 2-pixel marker is JPEG-diluted to ~220/190/120. Grey floor has tiny chroma;
    # blue frames and the simulator's brown furniture are outside this range.
    # Angled subpixel stripes can be diluted further (~216/196/133). Preserve
    # yellow hue rather than a high absolute saturation cutoff: orange/brown
    # furniture has a much larger red-green than green-blue difference.
    return (red >= 160 and green >= 135 and blue <= 180 and
            red-green >= 8 and green-blue >= 28 and red-blue >= 45 and
            green-blue >= 1.5*(red-green))


def _components(points):
    """Bridge at most one missing JPEG pixel; retain original measured pixels."""
    offsets = [(dx, dy) for dx in range(-2, 3) for dy in range(-2, 3) if dx or dy]
    while points:
        first = points.pop()
        pending, component = [first], [first]
        while pending:
            x, y = pending.pop()
            for dx, dy in offsets:
                neighbor = (x+dx, y+dy)
                if neighbor in points:
                    points.remove(neighbor)
                    pending.append(neighbor)
                    component.append(neighbor)
        yield component


def _edge_fit(columns, span):
    """Robust slope and intercept from measured upper pixels, not band centroid."""
    ordered = sorted((x, min(ys)) for x, ys in columns.items())
    sample = ordered[::max(1, len(ordered)//48)]
    slopes = [(by-ay)/(bx-ax) for index, (ax, ay) in enumerate(sample)
              for bx, by in sample[index+1:] if bx-ax >= max(12, span*.2)]
    if not slopes:
        return None
    slope = median(slopes)
    intercept = median(y-slope*x for x, y in ordered)
    residuals = sorted(abs(y-slope*x-intercept) for x, y in ordered)
    return slope, intercept, residuals[int(len(residuals)*.9)]


def detect_threshold(jpeg):
    """Return one unique shallow yellow floor band, otherwise None.

    x is the midpoint of its visible span. y is the fitted TOP EDGE at that x,
    not its centroid or a sloping segment's highest endpoint. Bounds cover the
    original pixels and are normalized, right/bottom exclusive.
    quality describes pixel/shape support, never semantic certainty or proximity.
    Tilt is not inferred: search covers y>.25 to support both level and lowered
    simulator cameras. No metric distance, goal flag or movement is produced.
    """
    if not isinstance(jpeg, bytes) or not jpeg or len(jpeg) > 5_000_000:
        return None
    try:
        with Image.open(io.BytesIO(jpeg)) as source:
            if source.format != 'JPEG' or source.size != (640, 480):
                return None
            image = source.convert('RGB')
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    width, height = image.size
    pixels = image.load()
    minimum = max(48, math.ceil(width*.10))
    points = {(x, y) for y in range(math.floor(height*.25)+1, height) for x in range(width)
              if _yellow(*pixels[x, y])}
    candidates = []
    for component in _components(points):
        columns = {}
        for x, y in component:
            columns.setdefault(x, []).append(y)
        left, right = min(columns), max(columns)+1
        span = right-left
        if span < minimum or len(columns)/span < .8:
            continue
        top, bottom = min(y for x, y in component), max(y for x, y in component)+1
        thickness = median(max(ys)-min(ys)+1 for ys in columns.values())
        fit = _edge_fit(columns, span)
        if (fit is None or top <= height*.25+2 or thickness > height*.2):
            continue
        slope, intercept, residual = fit
        if abs(slope) > .25 or residual > 3.5:
            continue
        center_x = (left+right-1)/2
        edge_y = slope*center_x+intercept
        if not top <= edge_y < bottom:
            continue
        # Close perspective enlarges the stripe even while its upper floor edge
        # stays perfectly localized. A full-width band cropped at the bottom
        # must not disappear at an arbitrary 8:1 aspect boundary. Permit a
        # slightly wider band ONLY there; retain the same 96px median-thickness,
        # hue, linearity, coverage and ambiguity checks. Uncropped color patches
        # still need the original thin-band aspect.
        thin_band = span/thickness >= 8
        clipped_near_band = (left == 0 and right == width and bottom == height
                            and edge_y >= height*.55 and span/thickness >= 6.5)
        if not (thin_band or clipped_near_band):
            continue
        density = sum(len(ys)/(max(ys)-min(ys)+1) for ys in columns.values())/len(columns)
        candidates.append(dict(x=(left+right)/(2*width), y=edge_y/height,
            bounds=dict(left=left/width, top=top/height, right=right/width, bottom=bottom/height),
            quality=round(min(1., .55+.2*density+.15*span/width+.1*(1-residual/3.5)), 4),
            provenance=PROVENANCE, measurement='top_edge_of_colored_threshold_band',
            slope_px_per_px=slope, edge_residual_px=residual,
            shape_support='thin_band' if thin_band else 'full_width_bottom_cropped_near_band'))
    # Never select the biggest of several plausible colored bands: identity
    # cannot be established from the color marker alone.
    return candidates[0] if len(candidates) == 1 else None
