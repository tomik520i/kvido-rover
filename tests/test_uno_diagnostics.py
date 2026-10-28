"""Bound the actual UNO diagnostic wire format without hardware or serial access."""
import json
from pathlib import Path
import re


def test_worst_case_uno_json_fits_transmitter_and_esp_receiver():
    root=Path(__file__).resolve().parents[1]
    uno=(root/'firmware/uno/main.cpp').read_text(encoding='utf-8')
    match=re.search(r'PSTR\((.*?)\),\s*current,',uno,re.S)
    assert match
    wire_format=''.join(json.loads(literal) for literal in re.findall(r'"(?:\\.|[^"\\])*"',match.group(1)))
    # Conservatively combine the longest values, including mutually exclusive states.
    u32=2**32-1
    fields=('null',145,'false',255,1023,1023,1023,2000,255,10,40,254,
            u32,u32,'false','false',u32,u32,'null',u32,u32,
            u32,255,255,255,65535,'false','invalid_direction',u32,
            u32,255,255,65535,'false','invalid_direction',u32,u32,u32,
            'false',255,u32,u32,'zero_direction',u32,u32,u32)
    packet=(wire_format.replace('%lu','%d').replace('%u','%d')%fields).encode('ascii')
    parsed=json.loads(packet)
    assert parsed['distance_cm'] is None and parsed['cmd']['ok'] is False
    assert parsed['ultra']['seq']==u32 and parsed['drive']['ms']==65535
    assert parsed['motor']['stop']=='zero_direction'
    assert packet.endswith(b'\n')
    capacity=int(re.search(r'char report\[(\d+)\]',uno).group(1))
    assert len(packet)<capacity
    assert parsed['motor_max_duration_ms']==2000
    assert parsed['forward_stop_cm']==10
    assert parsed['camera_clearance_min_cm']==40 and parsed['camera_clearance_cm']==254
    esp=(root/'firmware/esp32cam/main.cpp').read_text(encoding='utf-8')
    line_limit=int(re.search(r'serialLine.length\(\)<(\d+)',esp).group(1))
    assert len(packet)<line_limit
    # 8N1 wire capacity; telemetry leaves more than half of 115200 baud for overhead.
    assert len(packet)/.150 < 115200/10*.5
