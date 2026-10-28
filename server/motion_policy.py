"""Direction-specific limits shared by planning and the actuator boundary."""
import math

AI_WHEELS = ('forward', 'backward', 'left', 'right', 'rotate_left', 'rotate_right')
ESCAPE = ('backward', 'left', 'right', 'rotate_left', 'rotate_right')


def fresh(telemetry):
    age=telemetry.get('telemetry_age_ms')
    return (telemetry.get('connected') is not False and type(age) in (int,float)
            and math.isfinite(age) and 0<=age<=500)


def hardware_limits(telemetry):
    """Long steps require capabilities reported by both boards, not a setting."""
    duration=telemetry.get('motor_max_duration_ms')
    stop=telemetry.get('forward_stop_cm')
    if (fresh(telemetry) and type(duration) is int and 350<=duration<=2000
            and type(stop) in (int,float) and 10<=stop<=20):
        return duration,stop
    return 350,20


def hardware_speed_limit(telemetry):
    """Full PWM requires fresh, explicit agreement from BOTH physical boards."""
    return 255 if (telemetry.get('simulation') is False and fresh(telemetry)
        and type(telemetry.get('motor_max_speed')) is int and telemetry['motor_max_speed']==255
        and type(telemetry.get('esp_motor_max_speed')) is int and telemetry['esp_motor_max_speed']==255) else 180


def command_limits(action,policy,telemetry):
    maximum,_=hardware_limits(telemetry)
    if policy=='sensor':
        # The physical beta uses the maximum PWM accepted by the installed ESP
        # firmware at the operator's request. Simulator calibration is separate.
        speed=hardware_speed_limit(telemetry) if telemetry.get('simulation') is False else 150 if action.startswith('rotate_') else 180
        # Front range does not cover rear/sides: escape in bounded one-second steps.
        duration=min(maximum,1000 if action in ('backward','left','right') else 2000)
    else:
        speed,duration=(80,100) if action.startswith('rotate_') else (150,300)
    return speed,duration
