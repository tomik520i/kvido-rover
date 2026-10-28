import io
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from server.threshold_pixels import PROVENANCE, detect_threshold


def jpeg(painter=None):
    image = Image.new('RGB', (640, 480), (150, 141, 129))
    if painter:
        painter(ImageDraw.Draw(image))
    stream = io.BytesIO(); image.save(stream, 'JPEG', quality=85)
    return stream.getvalue()


def test_single_yellow_floor_band_returns_current_top_edge_and_normalized_bounds():
    result = detect_threshold(jpeg(lambda d:d.rectangle((180, 300, 460, 306), fill=(232, 192, 61))))
    assert result is not None
    assert abs(result['x']-.5) < .01 and abs(result['y']-300/480) < .006
    assert result['bounds']['top'] <= result['y'] < result['bounds']['bottom']
    assert result['bounds']['right'] > result['bounds']['left']
    assert result['bounds']['bottom'] > result['bounds']['top']
    assert result['provenance'] == PROVENANCE
    assert result['measurement'] == 'top_edge_of_colored_threshold_band'
    assert 0 < result['quality'] <= 1
    assert not {'goal_visible', 'confidence', 'at_threshold', 'action'} & result.keys()


@pytest.mark.parametrize('bounds', [(0, 440, 639, 479), (0, 330, 639, 337), (0, 180, 639, 186)])
def test_frame_spanning_and_bottom_cropped_marker_and_lowered_camera_positive(bounds):
    result = detect_threshold(jpeg(lambda d:d.rectangle(bounds, fill=(232, 192, 61))))
    assert result is not None
    assert abs(result['x']-.5) < .01
    assert abs(result['y']-bounds[1]/480) < .006
    assert result['bounds']['left'] == 0 and result['bounds']['right'] == 1


@pytest.mark.parametrize('painter', [
    None,
    lambda d:d.rectangle((280, 140, 293, 450), fill=(35, 91, 174)),
    lambda d:(d.rectangle((200, 300, 400, 420), fill=(166, 92, 56)),
              d.line((200, 300, 400, 300), fill=(180, 120, 60), width=4)),
    lambda d:d.rectangle((200, 300, 400, 420), fill=(232, 192, 61)),
    lambda d:d.line((40, 320, 600, 320), fill=(120, 120, 120), width=3),
    lambda d:d.rectangle((180, 50, 460, 56), fill=(232, 192, 61)),
    lambda d:(d.rectangle((60, 300, 250, 305), fill=(232, 192, 61)),
              d.rectangle((350, 380, 600, 385), fill=(232, 192, 61))),
    lambda d:d.polygon(((100, 300), (550, 360), (550, 440), (100, 380)), fill=(232, 192, 61)),
    lambda d:d.line((150, 200, 350, 420), fill=(232, 192, 61), width=5),
])
def test_nonmarker_and_ambiguous_images_abstain(painter):
    assert detect_threshold(jpeg(painter)) is None


def test_grey_floor_grid_cannot_be_mistaken_for_yellow_marker():
    def paint(draw):
        for y in range(250, 480, 25):
            draw.line((0, y, 639, y), fill=(190, 181, 169), width=3)
        for x in range(0, 640, 35):
            draw.line((x, 250, x, 479), fill=(105, 103, 97), width=2)
    assert detect_threshold(jpeg(paint)) is None


def test_two_separate_same_row_markers_are_ambiguous():
    result = detect_threshold(jpeg(lambda d:(
        d.rectangle((20, 300, 250, 306), fill=(232, 192, 61)),
        d.rectangle((350, 300, 620, 306), fill=(232, 192, 61)))))
    assert result is None


@pytest.mark.parametrize('color', [(232, 192, 61), (216, 196, 133)])
def test_shallow_sloped_diluted_yellow_marker_uses_top_edge_at_segment_midpoint(color):
    result = detect_threshold(jpeg(lambda d:d.polygon(
        ((100, 300), (550, 360), (550, 365), (100, 305)), fill=color)))
    assert result is not None
    assert abs(result['x']-325/640) < .01
    assert abs(result['y']-330/480) < .01
    assert result['y'] > result['bounds']['top']+.04
    assert abs(result['slope_px_per_px']-60/450) < .02


def test_two_shallow_sloped_markers_remain_ambiguous():
    result = detect_threshold(jpeg(lambda d:(
        d.line((20, 300, 250, 315), fill=(232, 192, 61), width=5),
        d.line((350, 380, 620, 395), fill=(232, 192, 61), width=5))))
    assert result is None


@pytest.mark.parametrize('step', [271,274,275,276,277,278,279])
def test_saved_shallow_marker_frames_localize_from_pixels_only(step):
    path = Path(__file__).resolve().parents[1]/'reports/sensor-gemma-localization-recovery-95676858'/f'step-{step:04d}-before.jpg'
    if not path.exists():
        pytest.skip('optional saved experiment JPEG unavailable')
    result = detect_threshold(path.read_bytes())
    assert result is not None
    assert .3 < result['y'] < .45
    assert result['edge_residual_px'] < 2
    assert 0 < result['slope_px_per_px'] < .2


@pytest.mark.parametrize('filename', ['step-5-before.jpg','step-13-after.jpg'])
def test_saved_no_target_camera_images_remain_negative(filename):
    path = Path(__file__).resolve().parents[1]/'reports/simulator-apartment-graph-gemma-02'/filename
    if not path.exists():
        pytest.skip('optional saved experiment JPEG unavailable')
    assert detect_threshold(path.read_bytes()) is None


@pytest.mark.parametrize('step',[280,281])
def test_saved_near_marker_does_not_disappear_as_perspective_band_widens(step):
    path = Path(__file__).resolve().parents[1]/'reports/sensor-gemma-marker-approach-de2ec36a'/f'step-{step:04d}-before.jpg'
    if not path.exists():
        pytest.skip('optional saved experiment JPEG unavailable')
    result=detect_threshold(path.read_bytes())
    assert result is not None
    assert result['x']==.5 and .64<result['y']<.70
    assert result['bounds']['left']==0 and result['bounds']['right']==1
    assert result['edge_residual_px']<1.5 and .1<result['slope_px_per_px']<.2
    if step==281:
        assert result['bounds']['bottom']==1
        assert result['shape_support']=='full_width_bottom_cropped_near_band'
        assert result['y']==pytest.approx(.68077,abs=.003)


def test_wide_near_cropped_sloped_marker_retains_measured_upper_edge_not_centroid():
    result=detect_threshold(jpeg(lambda d:d.polygon(
        ((0,277),(639,376),(639,486),(0,343)),fill=(232,192,61))))
    assert result is not None
    assert result['shape_support']=='full_width_bottom_cropped_near_band'
    assert result['bounds']['left']==0 and result['bounds']['right']==1 and result['bounds']['bottom']==1
    assert abs(result['y']-326.5/480)<.005
    assert abs(result['slope_px_per_px']-99/639)<.015


@pytest.mark.parametrize('bounds',[(0,395,639,479),(0,386,639,479)])
def test_full_width_bottom_crop_works_just_below_original_eight_to_one_aspect(bounds):
    result=detect_threshold(jpeg(lambda d:d.rectangle(bounds,fill=(232,192,61))))
    assert result is not None
    assert result['shape_support']=='full_width_bottom_cropped_near_band'
    assert result['y']==pytest.approx(bounds[1]/480,abs=.005)


@pytest.mark.parametrize('painter',[
    lambda d:d.rectangle((0,300,639,384),fill=(232,192,61)),  # wide but not cropped
    lambda d:d.rectangle((0,370,639,479),fill=(232,192,61)),  # too thick even at the edge
    lambda d:d.rectangle((0,395,639,479),fill=(166,92,56)),   # same shape, brown furniture color
    lambda d:(d.rectangle((0,395,639,479),fill=(232,192,61)),
              d.rectangle((40,300,600,305),fill=(232,192,61))), # two plausible candidates
])
def test_near_band_exception_does_not_remove_patch_color_or_ambiguity_guards(painter):
    assert detect_threshold(jpeg(painter)) is None


@pytest.mark.parametrize('value', [None, b'', b'notjpeg', 'data:image/jpeg;base64,x', b'x'*5_000_001],
                         ids=['none', 'empty', 'invalid', 'notbytes', 'oversized'])
def test_invalid_input_abstains(value):
    assert detect_threshold(value) is None
