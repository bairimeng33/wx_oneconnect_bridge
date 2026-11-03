"""Huawei ``fan`` service: fan gear and swing direction.

Default contract source
    prodId    : ``A0NQ`` -- wall-mounted air conditioner
    Profile   : https://smarthome-drcn.dbankcdn.com/device/guide/A0NQ/A0NQ.json
    common    : yes -- air conditioners expose ``gear`` / ``direction`` enums
    verified  : candidate, 2026-10-22 -- enum mapping checked against the Profile
                and running on the legacy bridge; not verified over this
                repository's projection yet. Promote after the five acceptance
                steps in ``docs/service-adapter-contributing.md``.

Profile enums
    gear      : ``0`` auto / ``1`` low / ``2`` medium / ``3`` high /
                ``4`` silent / ``5`` turbo
    direction : ``1`` fixed / ``2`` horizontal / ``3`` vertical / ``4`` both

Bound entity domain
    The adapter accepts two kinds of Home Assistant entities:

      ``climate.`` -- air conditioner.  ``gear`` is reported from ``fan_mode``
                      and swing from ``swing_mode`` / ``swing_horizontal_mode``.
      ``fan.``     -- household fan / ceiling fan / ceiling fan light.  ``gear``
                      is derived from the HA ``percentage`` attribute and issued
                      through ``fan.set_percentage``; ``speed`` can report the
                      same percentage on Profiles that expose a numeric speed;
                      oscillation is mapped to ``direction`` or ``angle`` via
                      ``oscillating``.

Contract
    ``gear_values``        : for ``climate.`` entities only, maps HA ``fan_mode``
                             names to gear ints.  Unsupported names are skipped.
    ``gear_percent``       : for ``fan.`` entities only, maps a gear int to the
                             HA ``percentage`` it should be reported as / set to.
                             When omitted a default 1->25 / 2->50 / 3->100 is used.
    ``gear_enabled``       : for ``fan.`` entities only, controls whether the
                             ``gear`` characteristic is reported / accepted.
                             Default true.  Set it to false for Profiles whose
                             ``fan`` service exposes only ``speed``.
    ``speed_enabled``      : for ``fan.`` entities only, report / accept the
                             numeric ``speed`` characteristic.  Default false.
    ``speed_min``          : minimum wire value of ``speed``.  Default 1.
    ``speed_max``          : maximum wire value of ``speed``.  Default 100.
    ``direction_enabled``  : controls whether the optional ``direction``
                             characteristic is read and reported.  Set it to false
                             for Profiles that expose only ``gear`` (most
                             household fans).
    ``angle_enabled``      : for ``fan.`` entities only, use the ``angle``
                             characteristic instead of ``direction`` for
                             oscillation.  Default false.
    ``angle_on``           : wire ``angle`` value when oscillating.  Default 1.
    ``angle_off``          : wire ``angle`` value when not oscillating.
                             Default 0.

Details
    For ``climate.`` entities, swing is reported from Home Assistant's two swing
    attributes: newer releases expose ``swing_mode`` (vertical) and
    ``swing_horizontal_mode`` (horizontal), older releases only ``swing_mode``
    with ``on`` meaning vertical.

    For ``fan.`` entities, the HA ``percentage`` (0..100) maps linearly onto
    ``speed_min``..``speed_max`` when ``speed_enabled`` is set, e.g. ``1..32``
    for a 32-step fan or ``1..100`` when the Profile uses percent.  When both
    ``gear`` and ``speed`` are enabled both are reported, but a downstream
    command prefers ``speed`` because it carries the finer value.

    For ``fan.`` entities swing is derived from ``oscillating``.  With
    ``angle_enabled`` it maps to ``angle_on`` / ``angle_off`` (covers both the
    ``0`` hold / ``1`` sweep enum and continuous angles such as ``0`` / ``90``);
    otherwise it maps to ``direction=3`` (vertical) / ``direction=1`` (fixed).

    Unmapped gear, an out-of-range speed, an unknown angle or an unsupported
    swing name is skipped instead of forwarded, so the cloud never receives a
    value it would reject for the whole service.
    The on/off part of a household fan is normally bound separately through the
    ``switch`` adapter, which already accepts ``fan.`` domain entities.

Source
    This adapter is an extension of the ``fan`` adapter originally contributed
    to the ``ha-oneconnect-bridge`` project
    (https://github.com/xiasi0/ha-oneconnect-bridge), extended to additionally
    support ``fan.`` domain entities for household fans and ceiling fan lights.
"""

from __future__ import annotations

from ..models import ServiceRef

_CLIMATE_DEFAULT_GEAR_TO_HA = {
    0: "auto",
    1: "low",
    2: "medium",
    3: "high",
    4: "silent",
    5: "turbo",
}

# Default gear -> HA percentage mapping for household ``fan.`` entities when no
# ``gear_percent`` contract override is given.
_FAN_DEFAULT_GEAR_PERCENT = {
    1: 25,
    2: 50,
    3: 100,
}

_DIRECTION_TO_HA = {1: "off", 2: "horizontal", 3: "vertical", 4: "both"}
_HA_TO_DIRECTION = {name: direction for direction, name in _DIRECTION_TO_HA.items()}


class FanServiceAdapter:
    service_id = "fan"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type != "fan":
            raise ValueError("fan requires st: fan")
        unknown = set(dict(service.contract)) - {
            "gear_values",
            "gear_percent",
            "gear_enabled",
            "direction_enabled",
            "speed_enabled",
            "speed_min",
            "speed_max",
            "angle_enabled",
            "angle_on",
            "angle_off",
        }
        if unknown:
            raise ValueError(
                f"fan has unsupported contract keys: {', '.join(sorted(unknown))}"
            )
        _gear_values(service)
        _gear_percent(service)
        _gear_enabled(service)
        _direction_enabled(service)
        _speed_enabled(service)
        _speed_limits(service)
        _angle_enabled(service)
        _angle_values(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("fan requires exactly one entity")
        domain = entity_ids[0].partition(".")[0]
        if domain not in {"climate", "fan"}:
            raise ValueError("fan requires a climate or fan entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None:
            return {}
        attributes = state.attributes
        domain = entity_ids[0].partition(".")[0]
        data: dict[str, object] = {}

        if domain == "climate":
            if _gear_enabled(service):
                gear = _gear_values(service).get(attributes.get("fan_mode"))
                if gear is not None:
                    data["gear"] = gear
        else:
            percentage = attributes.get("percentage")
            if _gear_enabled(service):
                gear = _fan_gear_from_percentage(
                    _gear_percent(service), percentage
                )
                if gear is not None:
                    data["gear"] = gear
            if _speed_enabled(service):
                speed = _fan_speed_from_percentage(
                    percentage, *_speed_limits(service)
                )
                if speed is not None:
                    data["speed"] = speed

        if domain == "climate":
            if not _direction_enabled(service):
                return data
            vertical = _active(attributes.get("swing_mode"))
            horizontal = _active(attributes.get("swing_horizontal_mode"))
            if vertical and horizontal:
                data["direction"] = 4
            elif vertical:
                data["direction"] = 3
            elif horizontal:
                data["direction"] = 2
            else:
                data["direction"] = 1
        elif _angle_enabled(service):
            angle_on, angle_off = _angle_values(service)
            data["angle"] = (
                angle_on if _active(attributes.get("oscillating")) else angle_off
            )
        elif _direction_enabled(service):
            data["direction"] = 3 if _active(attributes.get("oscillating")) else 1
        return data

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        attributes = state.attributes if state else {}
        domain = entity_ids[0].partition(".")[0]
        gear = _wire_int(payload.get("gear")) if _gear_enabled(service) else None

        if domain == "climate":
            direction = (
                _wire_int(payload.get("direction"))
                if _direction_enabled(service)
                else None
            )
            gear_to_ha = {value: name for name, value in _gear_values(service).items()}
            fan_mode = gear_to_ha.get(gear) if gear is not None else None
            swing_mode = _DIRECTION_TO_HA.get(direction) if direction is not None else None
            if (gear is not None and fan_mode is None) or (
                direction is not None and swing_mode is None
            ):
                return
            if fan_mode is not None:
                if not _allowed(attributes.get("fan_modes"), fan_mode):
                    return
                await hass.services.async_call(
                    "climate",
                    "set_fan_mode",
                    {"entity_id": entity_ids[0], "fan_mode": fan_mode},
                    blocking=True,
                )
            if swing_mode is not None:
                if not _allowed(attributes.get("swing_modes"), swing_mode):
                    return
                await hass.services.async_call(
                    "climate",
                    "set_swing_mode",
                    {"entity_id": entity_ids[0], "swing_mode": swing_mode},
                    blocking=True,
                )
            return

        # Home Assistant ``fan.`` entity (household fan / ceiling fan light).
        # A numeric ``speed`` carries the finer value, so it wins over ``gear``
        # when both are present in the same downstream command.
        speed = _wire_int(payload.get("speed")) if _speed_enabled(service) else None
        percentage: int | None = None
        if speed is not None:
            speed_min, speed_max = _speed_limits(service)
            if not speed_min <= speed <= speed_max:
                return
            percentage = _fan_percentage_from_speed(speed, speed_min, speed_max)
        elif gear is not None:
            percentage = _gear_percent(service).get(gear)
            if percentage is None:
                return
        if percentage is not None:
            await hass.services.async_call(
                "fan",
                "set_percentage",
                {"entity_id": entity_ids[0], "percentage": percentage},
                blocking=True,
            )

        if _angle_enabled(service):
            angle = _wire_int(payload.get("angle"))
            if angle is not None:
                angle_on, angle_off = _angle_values(service)
                if angle == angle_on:
                    oscillating = True
                elif angle == angle_off:
                    oscillating = False
                else:
                    return
                await hass.services.async_call(
                    "fan",
                    "oscillate",
                    {"entity_id": entity_ids[0], "oscillating": oscillating},
                    blocking=True,
                )
            return

        if _direction_enabled(service):
            direction = _wire_int(payload.get("direction"))
            if direction is not None:
                await hass.services.async_call(
                    "fan",
                    "oscillate",
                    {"entity_id": entity_ids[0], "oscillating": direction >= 2},
                    blocking=True,
                )


ADAPTER = FanServiceAdapter()


def _gear_values(service: ServiceRef) -> dict[str, int]:
    """Parse ``gear_values`` (climate: HA fan_mode name -> gear int)."""
    raw = service.contract_value(
        "gear_values",
        {name: value for value, name in _CLIMATE_DEFAULT_GEAR_TO_HA.items()},
    )
    if not isinstance(raw, dict) or not raw:
        raise ValueError("fan contract gear_values must be a non-empty mapping")
    values: dict[str, int] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("fan contract gear_values must use non-empty text keys")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("fan contract gear_values must be integers")
        values[name] = value
    if len(set(values.values())) != len(values):
        raise ValueError("fan contract gear_values must be unique")
    return values


def _gear_percent(service: ServiceRef) -> dict[int, int]:
    """Parse ``gear_percent`` (household fan: gear int -> HA percentage)."""
    raw = service.contract_value("gear_percent", _FAN_DEFAULT_GEAR_PERCENT)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("fan contract gear_percent must be a non-empty mapping")
    values: dict[int, int] = {}
    for name, value in raw.items():
        try:
            gear = int(name)
        except (TypeError, ValueError):
            raise ValueError("fan contract gear_percent keys must be integer gears") from None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("fan contract gear_percent values must be integers")
        if not 0 <= value <= 100:
            raise ValueError("fan contract gear_percent values must be 0..100 percentages")
        values[gear] = value
    if len(set(values.values())) != len(values):
        raise ValueError("fan contract gear_percent values must be unique")
    return values


def _gear_enabled(service: ServiceRef) -> bool:
    value = service.contract_value("gear_enabled", True)
    if not isinstance(value, bool):
        raise ValueError("fan contract gear_enabled must be boolean")
    return value


def _direction_enabled(service: ServiceRef) -> bool:
    value = service.contract_value("direction_enabled", True)
    if not isinstance(value, bool):
        raise ValueError("fan contract direction_enabled must be boolean")
    return value


def _speed_enabled(service: ServiceRef) -> bool:
    value = service.contract_value("speed_enabled", False)
    if not isinstance(value, bool):
        raise ValueError("fan contract speed_enabled must be boolean")
    return value


def _speed_limits(service: ServiceRef) -> tuple[int, int]:
    minimum = service.contract_value("speed_min", 1)
    maximum = service.contract_value("speed_max", 100)
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (minimum, maximum)
    ):
        raise ValueError("fan contract speed_min and speed_max must be integers")
    if not 0 <= minimum < maximum:
        raise ValueError("fan contract speed_min must be below speed_max")
    return minimum, maximum


def _angle_enabled(service: ServiceRef) -> bool:
    value = service.contract_value("angle_enabled", False)
    if not isinstance(value, bool):
        raise ValueError("fan contract angle_enabled must be boolean")
    return value


def _angle_values(service: ServiceRef) -> tuple[int, int]:
    on = service.contract_value("angle_on", 1)
    off = service.contract_value("angle_off", 0)
    if any(
        isinstance(value, bool) or not isinstance(value, int) for value in (on, off)
    ):
        raise ValueError("fan contract angle_on and angle_off must be integers")
    if on == off:
        raise ValueError("fan contract angle_on and angle_off must differ")
    return on, off


def _fan_gear_from_percentage(
    gear_to_percent: dict[int, int], percentage: object
) -> int | None:
    """Map an HA ``fan.percentage`` (0..100) to the nearest configured gear."""
    if percentage is None or isinstance(percentage, bool):
        return None
    try:
        current = float(percentage)
    except (TypeError, ValueError):
        return None
    best_gear: int | None = None
    best_diff: float | None = None
    for gear, percent in gear_to_percent.items():
        diff = abs(current - percent)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_gear = gear
    return best_gear


def _fan_speed_from_percentage(
    percentage: object, minimum: int, maximum: int
) -> int | None:
    """Map an HA ``fan.percentage`` (0..100) onto the configured speed range."""
    if percentage is None or isinstance(percentage, bool):
        return None
    try:
        current = float(percentage)
    except (TypeError, ValueError):
        return None
    current = min(100.0, max(0.0, current))
    return round(minimum + current * (maximum - minimum) / 100)


def _fan_percentage_from_speed(speed: int, minimum: int, maximum: int) -> int:
    """Map a wire speed value back to the HA ``percentage`` range."""
    return round((speed - minimum) * 100 / (maximum - minimum))


def _active(value: object) -> bool:
    return value not in (None, "", "off", "none")


def _allowed(allowed: object, value: str) -> bool:
    """A command is skipped when the entity advertises a list that lacks it."""
    if isinstance(allowed, list) and allowed:
        return value in allowed
    return True


def _wire_int(value: object) -> int | None:
    """Accept only wire-plausible numbers (int or integral float); reject bool/str."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
