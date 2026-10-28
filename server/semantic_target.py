"""Pure resolver for a *simulation-only* current-image semantic observation.

The pinhole, camera height and motion constants below describe this simulator.
The result is one bounded command candidate, not motor authority or a verified
goal. Its metric estimate assumes the model identified the actual target's
threshold on a flat floor. It cannot establish that the visual claim is true,
and provides no guarantees for a physical vehicle. No pose/map/GT is consumed.

Callers retain independent permission, collision and post-action verification
guards. Existing stop/tilt decisions must not be sent here to become movement.
"""
import math


IMAGE_WIDTH = 640.
IMAGE_HEIGHT = 480.
FOCAL_LENGTH = 280.
CAMERA_HEIGHT_M = .22
STOP_DISTANCE_M = .30
RANGE_MARGIN_M = .03
CANDIDATE_DISTANCE_M = .35
TARGET_DEADBAND = .04
ROTATION_SPEED = 150
ROTATION_RATE_RAD_S = 1.2 * (ROTATION_SPEED - 45) / (180 - 45)
FORWARD_SPEED = 180
CONSERVATIVE_SPEED_M_S = .40  # simulated maximum is .35 m/s
SENSOR_STOP_M = .10
SENSOR_MARGIN_M = .03
MIN_PULSE_MS = 50
PROVENANCE = 'model_perception_calibrated_simulation_estimate'


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _unit(value):
    return _number(value) and 0 <= value <= 1


def _command(action, reason, *, servo=90, duration_ms=100,
             estimated_range_m=None, heading_error_rad=None, candidate=False,
             semantic_phase=None, handoff_pending=False, navigation_block=None):
    return {'action': action,
            'speed': ROTATION_SPEED if action.startswith('rotate_') else
                     FORWARD_SPEED if action == 'forward' else 0,
            'duration_ms': duration_ms, 'tilt': round(servo),
            'reason': reason, 'candidate_goal_reached': candidate,
            'provenance': PROVENANCE, 'simulation_only': True,
            'estimated_range_m': estimated_range_m,
            'heading_error_rad': heading_error_rad,
            'semantic_phase': semantic_phase,
            'navigation_block': navigation_block,
            'goal_handoff_pending': handoff_pending}


def resolve_semantic_target(perception, telemetry, *, floor_localized=True):
    """Return one command dict, or None when no confident goal is observed.

    Pixels are normalized current-frame coordinates; threshold_pixel is the
    threshold's y scalar. Bearing/at_threshold alone never supply missing pixels.
    Fresh simulated telemetry and a valid current servo are required even for a
    calibration tilt. A localized opening may authorize only bounded alignment
    before its threshold is visible. Only a centered target with floor geometry can
    generate forward or a candidate completion; neither is GT verification.
    """
    if not isinstance(perception, dict):
        return None
    if type(floor_localized) is not bool:
        raise ValueError('floor_localized must be boolean')
    hazard = perception.get('hazard')
    if hazard in ('person', 'animal', 'drop'):
        return _command('stop', 'Modelové pozorování hlásí konkrétní nebezpečí; stojím.')
    confidence = perception.get('confidence')
    if perception.get('goal_visible') is not True or not _unit(confidence) or confidence < .8:
        return None

    def stop(reason, **fields):
        return _command('stop', reason, **fields)

    # A perception is not a general-purpose motor decision. Do not turn a
    # legacy stop/tilt (or another supplied action) into semantic locomotion.
    if 'action' in perception:
        return stop('Vstup obsahuje původní povel; sémantický odhad jej nemění v jízdu.')
    t = telemetry if isinstance(telemetry, dict) else {}
    if t.get('simulation') is not True:
        return stop('Optická kalibrace platí pouze pro výslovně označenou simulaci.')
    age, servo = t.get('telemetry_age_ms'), t.get('servo')
    if (t.get('connected') is False or not _number(age) or not 0 <= age <= 500 or
            not _number(servo) or not 35 <= servo <= 145):
        return stop('Chybí čerstvá telemetrie do 500 ms nebo platný aktuální náklon kamery.')
    if hazard not in ('clear', 'obstacle', 'unknown'):
        return stop('Modelové pozorování nemá platný stav nebezpečí.', servo=servo)

    visible = perception.get('threshold_visible')
    threshold = perception.get('threshold_pixel')
    if type(visible) is not bool or type(perception.get('at_threshold')) is not bool:
        return stop('Příznaky viditelnosti prahu nejsou platné.', servo=servo)
    point = perception.get('target_pixel')
    localized = (isinstance(point, dict) and set(point) == {'x', 'y'} and
                 _unit(point.get('x')) and _unit(point.get('y')))
    if not floor_localized:
        # The current model can orient the camera to an opening, but its
        # ungrounded floor coordinates never authorize translation/completion.
        if localized and hazard == 'clear':
            alignment = _align_goal(point['x'], servo)
            if alignment is not None:
                return alignment
        return _command('tilt' if abs(servo-55)>.5 else 'stop',
            'Současný cíl znovu zaměřím; přesné měření prahu zatím chybí.',
            servo=55, semantic_phase='goal_alignment', handoff_pending=True)
    if not visible:
        if threshold is not None or perception.get('at_threshold'):
            return stop('Tvrzení o blízkém prahu nemá odpovídající obrazový bod.', servo=servo)
        if localized and hazard == 'clear':
            alignment = _align_goal(point['x'], servo)
            if alignment is not None:
                return alignment
        # Keep ownership of a currently observed goal while obtaining its floor
        # geometry. This is an explicit stopped observation, never permission
        # for an exploration controller to translate toward an unmeasured goal.
        if abs(servo - 55) > .5:
            return _command('tilt', 'Cíl je podle modelu vidět, ale práh ne; sklopím pouze kameru a vyžádám nový obraz.',
                            servo=55, semantic_phase='goal_alignment', handoff_pending=True)
        return stop('Cíl je podle modelu vidět, ale jeho práh není změřen; stojím a potřebuji nové pozorování cíle.',
                    servo=servo, semantic_phase='goal_alignment', handoff_pending=True)
    if not _unit(threshold):
        return stop('Viditelný práh nemá platnou normalizovanou svislou souřadnici.', servo=servo)
    if not localized:
        return stop('Cíl nemá platný obrazový střed; bearing ani at_threshold jej nenahradí.', servo=servo)

    x = point['x']
    heading = math.atan((.5 - x) * IMAGE_WIDTH / FOCAL_LENGTH)
    alignment = _align_goal(x, servo)
    if alignment is not None:
        return alignment

    horizon = IMAGE_HEIGHT / 2 - math.tan(math.radians((90 - servo) * .55)) * FOCAL_LENGTH
    below_horizon = threshold * IMAGE_HEIGHT - horizon
    if below_horizon <= 1:
        return stop('Práh neleží měřitelně pod kalibrovaným horizontem; vzdálenost neodhaduji.', servo=servo)
    estimated_range = CAMERA_HEIGHT_M * FOCAL_LENGTH / below_horizon
    if hazard != 'clear':
        return stop('Obraz nepotvrzuje volný prostor k cíli.', servo=servo, estimated_range_m=estimated_range)
    if estimated_range <= CANDIDATE_DISTANCE_M + 1e-12:
        return stop('Kalibrovaný odhad v simulaci ukazuje blízký práh; pouze kandidát dokončení, ověř novým pozorováním.',
                    servo=servo, estimated_range_m=estimated_range, candidate=True)

    distance = t.get('distance_cm')
    capability = t.get('motor_max_duration_ms', 1000)
    if not _number(distance) or not 10 < distance <= 400:
        return stop('Vpřed chybí platné čerstvé čelní měření nad 10 cm.', servo=servo,
                    estimated_range_m=estimated_range, navigation_block='front_sensor')
    if not _number(capability) or capability < MIN_PULSE_MS:
        return stop('Délka povelu není podporována platnou schopností simulátoru.', servo=servo, estimated_range_m=estimated_range)
    # Budget both independent observations; never round a too-small budget up.
    # Keep 30 cm + 3 cm for threshold uncertainty and 10 cm + 3 cm at sonar.
    # The 35 cm candidate band is wider than this reserve, leaving enough room
    # for one 50 ms pulse just outside it instead of stalling at 35--37 cm.
    range_budget = (estimated_range - STOP_DISTANCE_M - RANGE_MARGIN_M) / CONSERVATIVE_SPEED_M_S * 1000
    sensor_budget = (distance / 100 - SENSOR_STOP_M - SENSOR_MARGIN_M) / CONSERVATIVE_SPEED_M_S * 1000
    duration = math.floor(min(1000, capability, range_budget, sensor_budget))
    if duration < MIN_PULSE_MS:
        return stop('Rezerva nestačí ani na nejkratší omezený krok; stojím a potřebuji nové ověření.',
                    servo=servo, estimated_range_m=estimated_range)
    return _command('forward', 'Jediný krátký krok k modelovému cíli omezuje čidlo i kalibrovaný odhad prahu v simulaci; výsledek je nutné znovu ověřit.',
                    servo=servo, duration_ms=duration, estimated_range_m=estimated_range, heading_error_rad=heading)


def _align_goal(x, servo):
    """Align only to a validated current-image point; infer no floor range."""
    if abs(x - .5) <= TARGET_DEADBAND + 1e-12:
        return None
    heading = math.atan((.5 - x) * IMAGE_WIDTH / FOCAL_LENGTH)
    duration = max(MIN_PULSE_MS, min(500, round(abs(heading) / ROTATION_RATE_RAD_S * 1000)))
    return _command('rotate_left' if heading > 0 else 'rotate_right',
                    'Aktuální modelový bod cíle je mimo střed; pouze krátké natočení podle simulované optiky, pak nový snímek.',
                    servo=servo, duration_ms=duration, heading_error_rad=heading,
                    semantic_phase='goal_alignment', handoff_pending=True)
