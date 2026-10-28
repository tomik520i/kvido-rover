import pytest
from test_safety import a,c

@pytest.mark.parametrize('simulation,both,expected',[(False,True,255),(False,False,180),(True,True,180)])
def test_manual_pwm_uses_hardware_cap_and_simulator_limit(c,monkeypatch,simulation,both,expected):
    monkeypatch.setitem(a.settings,'simulation',simulation)
    async def telemetry():
        return dict(distance_cm=100,telemetry_age_ms=0,simulation=simulation,
            motor_max_speed=255,esp_motor_max_speed=255 if both else 180)
    sent=[]
    async def send(action,speed=0,duration_ms=100,tilt=90):sent.append((action,speed))
    monkeypatch.setattr(a,'telemetry',telemetry);monkeypatch.setattr(a,'send_control',send)
    assert c.post('/api/arm',json={}).status_code==200
    sent.clear()
    response=c.post('/api/drive',json={'action':'forward','speed':255,'duration_ms':100})
    assert response.status_code==200,response.text
    assert sent==[('forward',expected)]
