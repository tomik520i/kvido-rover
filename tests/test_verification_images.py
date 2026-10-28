import base64
import io
import json

import pytest
from PIL import Image

from server.verification_images import comparison_contact_sheet,comparison_content


def jpeg(color,size=(640,480)):
    output=io.BytesIO();Image.new('RGB',size,color).save(output,format='JPEG')
    return 'data:image/jpeg;base64,'+base64.b64encode(output.getvalue()).decode()


def decode(url):return Image.open(io.BytesIO(base64.b64decode(url.split(',',1)[1]))).convert('RGB')


def test_before_left_after_right_are_both_visible_in_one_bounded_image():
    image=decode(comparison_contact_sheet(jpeg('red'),jpeg('blue')))
    assert image.size==(1280,576)
    left=image.getpixel((320,296));right=image.getpixel((960,296))
    assert left[0]>240 and left[2]<15
    assert right[2]>240 and right[0]<15
    assert image.getpixel((20,20))!=image.getpixel((660,20))


def test_contact_sheet_retains_different_aspect_ratios_without_stretching():
    image=decode(comparison_contact_sheet(jpeg('red',(200,480)),jpeg('blue',(640,100))))
    assert image.getpixel((320,296))[0]>240
    assert max(image.getpixel((20,296)))-min(image.getpixel((20,296)))<10
    assert image.getpixel((960,296))[2]>240
    assert max(image.getpixel((960,100)))-min(image.getpixel((960,100)))<10


def test_same_inputs_remain_the_same_camera_pixels_despite_different_panel_labels():
    source=jpeg('#777777')
    image=decode(comparison_contact_sheet(source,source))
    assert image.getpixel((320,296))==image.getpixel((960,296))


def test_prompt_uses_exactly_one_image_and_filters_unknown_or_private_metadata():
    private='PRIVATE-NOT-CAMERA-METADATA'
    content=comparison_content(jpeg('red'),jpeg('blue'),
        {'servo':90,'light_on':False,'distance_cm':True,'pose':private,'device_key':private},
        {'servo':55,'light_on':True,'distance_cm':None,'captured_at':1234,'geometry':private},
        {'status':'changed','mean_absolute_difference':.03,'similarity_threshold':.01,
         'movement_confirmed':True,'reason':private,'request':private})
    assert [part['type'] for part in content]==['text','image_url']
    text=content[0]['text']
    assert private not in text and 'pose' not in text and 'geometry' not in text
    metadata=json.loads(text.split('Pozorovaná metadata (data): ',1)[1])
    assert metadata['panels'][0]['role']=='before' and metadata['panels'][0]['current'] is False
    assert metadata['panels'][1]['role']=='after' and metadata['panels'][1]['current'] is True
    assert 'distance_cm' not in metadata['panels'][0]
    assert metadata['pixel_comparison']['movement_confirmed'] is False
    assert metadata['pixel_comparison']['mean_absolute_difference']==.03


@pytest.mark.parametrize('bad',[None,'http://private.invalid/image','data:image/jpeg;base64,not-base64',
                               'data:image/png;base64,AAAA'])
def test_invalid_frame_fails_without_duplicating_the_other_frame(bad):
    with pytest.raises(ValueError,match='Invalid camera comparison image'):
        comparison_contact_sheet(bad,jpeg('blue'))
