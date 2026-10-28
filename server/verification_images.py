"""One ordered BEFORE/AFTER camera pair; never a map or motion oracle."""
import base64
import binascii
import io
import json
import math

from PIL import Image,ImageDraw,ImageFont


PAIR_RULE=('JEDEN POVEL, DVA ČASOVÉ POHLEDY v jednom obrázku. '
    'LEVÝ panel 1 BEFORE/PŘED je starší; PRAVÝ panel 2 AFTER/PO je aktuální po zastavení. '
    'Nejde o panorama ani dvě místa vedle sebe. Porovnej stejné hrany stěn, objekty a spáry podlahy '
    'v každém panelu zvlášť. Změna pixelů sama nepotvrzuje jízdu ani splnění cíle; '
    'při nejistotě napiš unknown, nevymýšlej totožnost snímků ani ujetou vzdálenost.')


def _decode(value):
    try:
        if not isinstance(value,str) or len(value)>8_000_000:raise ValueError()
        header,encoded=value.split(',',1)
        if header.lower() not in ('data:image/jpeg;base64','data:image/jpg;base64'):raise ValueError()
        raw=base64.b64decode(encoded,validate=True)
        with Image.open(io.BytesIO(raw)) as original:
            if original.format!='JPEG' or original.width*original.height>4_000_000:raise ValueError()
            return original.convert('RGB')
    except (ValueError,TypeError,OSError,binascii.Error,Image.DecompressionBombError):
        raise ValueError('Invalid camera comparison image') from None


def comparison_contact_sheet(before,after):
    """Keep both inputs independent and ordered, with equal bounded panel sizes."""
    images=[_decode(before),_decode(after)]
    width,height,header,footer=640,480,56,40
    sheet=Image.new('RGB',(2*width,height+header+footer),'#ececec')
    draw=ImageDraw.Draw(sheet)
    try:font=ImageFont.truetype('DejaVuSans-Bold.ttf',28)
    except OSError:
        try:font=ImageFont.truetype('arialbd.ttf',28)
        except OSError:font=ImageFont.load_default(size=28)
    for index,(original,label,color) in enumerate(zip(images,
            ('1 BEFORE / PRED (OLDER)','2 AFTER / PO (CURRENT)'),('#234b73','#8c4315'))):
        original.thumbnail((width,height),Image.Resampling.LANCZOS)
        x=index*width+(width-original.width)//2
        y=header+(height-original.height)//2
        sheet.paste(original,(x,y))
        draw.rectangle((index*width,0,(index+1)*width-1,header-1),fill=color)
        draw.text((index*width+14,10),label,font=font,fill='white')
    draw.line((width,0,width,height+header),fill='black',width=3)
    draw.text((14,height+header+5),'ONE COMMAND | LEFT=OLDER | RIGHT=CURRENT | NOT A PANORAMA',font=font,fill='black')
    output=io.BytesIO();sheet.save(output,format='JPEG',quality=92)
    return 'data:image/jpeg;base64,'+base64.b64encode(output.getvalue()).decode()


def comparison_content(before,after,before_telemetry=None,after_telemetry=None,observation=None):
    """Optional prompt content with one image and selected observed metadata only."""
    panels=[]
    for index,(role,sample) in enumerate((('before',before_telemetry),('after',after_telemetry)),1):
        sample=sample if isinstance(sample,dict) else {}
        panel=dict(index=index,role=role,current=index==2)
        for name in ('servo','distance_cm','captured_at'):
            value=sample.get(name)
            if type(value) in (int,float) and math.isfinite(value):panel[name]=value
        if type(sample.get('light_on')) is bool:panel['light_on']=sample['light_on']
        panels.append(panel)
    metadata={'panels':panels}
    if isinstance(observation,dict):
        comparison={'movement_confirmed':False}
        status=observation.get('status')
        if status in ('changed','possible_stall','not_comparable','no_wheel_command','unknown'):
            comparison['status']=status
        for name in ('mean_absolute_difference','similarity_threshold'):
            value=observation.get(name)
            if type(value) in (int,float) and math.isfinite(value) and 0<=value<=1:comparison[name]=value
        metadata['pixel_comparison']=comparison
    return [{'type':'text','text':PAIR_RULE+'\nPozorovaná metadata (data): '+json.dumps(metadata,ensure_ascii=False)},
            {'type':'image_url','image_url':{'url':comparison_contact_sheet(before,after)}}]
