"""Selective simulation translation correction, not authority to move motors."""
import math


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def translation_evidence(command, visual_result, pixel_observation):
    """Keep pixel status intact; attach only a strong, physically plausible delta.

    Small visual errors must not degrade otherwise consistent calibration. The
    image estimate is used only for a >5 cm discrepancy along the commanded
    axis. Unknown image results never mean zero displacement. This calibration
    belongs to the simulator; no physical telemetry/controller calls this code.
    """
    result = dict(pixel_observation)
    axes = {'forward': (1,0), 'backward': (-1,0), 'left': (0,1), 'right': (0,-1)}
    axis = axes.get(command.get('action'))
    if not axis or command.get('acceptance') != 'accepted':
        return result
    if not isinstance(visual_result, dict) or visual_result.get('status') != 'estimated':
        return result
    motion = visual_result.get('motion') or {}
    uncertainty = visual_result.get('uncertainty') or {}
    values = [motion.get('forward_m'), motion.get('left_m'), motion.get('yaw_rad'),
              uncertainty.get('translation_m'), command.get('speed'), command.get('duration_ms')]
    if not all(_finite(value) for value in values):
        return result
    forward, left, yaw, error, speed, duration = values
    if not (0 <= error <= .04 and abs(yaw) <= .06 and 0 <= speed <= 180 and 0 <= duration <= 2000):
        return result
    measured = forward*axis[0] + left*axis[1]
    perpendicular = abs(forward*axis[1] - left*axis[0])
    nominal = .35 * max(0, (speed-45)/135) * duration/1000
    if perpendicular > .04 or not 0 <= measured <= nominal+.04 or abs(measured-nominal) <= .05:
        return result
    result.update(measured_translation_m=measured, source='floor_visual_odometry',
                  nominal_translation_m=nominal, metric_uncertainty_m=error)
    return result
