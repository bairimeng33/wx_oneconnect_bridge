"""华为 ``volume`` / ``volSwitch`` 服务：媒体播放器的音量与静音。

来源
    ``volume``    : prodId ``22C1``，``volume`` int 0..100 RW。
    ``volSwitch`` : prodId ``2GOX``，``on`` bool RW，``1`` 表示静音。
    两个 Profile 均来自华为 IoT 目录；本仓库尚未在真实投影上验收，按
    ``docs/service-adapter-contributing.md`` 标记为“未验证”。

HA 转换
    只接受 ``media_player.`` 域，且只覆盖电视 / 媒体播放器的开关、静音、音量：

      ``volume``    上报 ``volume_level``（0..1）换算成的整数音量；
                    下行调用 ``media_player.volume_set``。
      ``volSwitch`` 上报 ``is_volume_muted``，``on`` 即静音；
                    下行调用 ``media_player.volume_mute``。

    播放 / 暂停、曲目信息、信号源等不在本适配器范围内：这些能力在 Profile 中
    通常属于 ``player`` / ``action`` / ``remotecontrol`` 等其它服务。
    电视电源一般由同一个 Profile 的 ``switch`` 服务承担，并绑定同一个
    ``media_player.`` 实体（``switch`` 适配器已支持该域）。

contract
    ``volume`` 服务支持 ``min`` / ``max`` / ``step``（默认 0 / 100 / 1）。
    ``volSwitch`` 服务没有契约项。
"""

from __future__ import annotations

from ..models import ServiceRef

_MUTE_SERVICE_IDS = {"volSwitch"}
_VOLUME_SERVICE_IDS = {"volume"}


class MediaPlayerServiceAdapter:
    service_id = "volume"

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_id in _MUTE_SERVICE_IDS:
            # 多数 Profile 把 volSwitch 声明为 serviceType=switch。
            if service.service_type not in {"switch", "mute"}:
                raise ValueError("volSwitch requires st: switch or mute")
            unknown = set(dict(service.contract))
            if unknown:
                raise ValueError(
                    "volSwitch has unsupported contract keys: "
                    f"{', '.join(sorted(unknown))}"
                )
            return

        if service.service_id not in _VOLUME_SERVICE_IDS:
            raise ValueError(
                f"media_player does not support service {service.service_id}"
            )
        if service.service_type != "volume":
            raise ValueError("volume requires st: volume")
        unknown = set(dict(service.contract)) - {"min", "max", "step"}
        if unknown:
            raise ValueError(
                f"volume has unsupported contract keys: {', '.join(sorted(unknown))}"
            )
        _limits(service)

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1 or not entity_ids[0].startswith("media_player."):
            raise ValueError("media player services require exactly one media_player entity")

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None or state.state in {"unknown", "unavailable"}:
            return {}
        attributes = state.attributes

        if service.service_id in _MUTE_SERVICE_IDS:
            muted = attributes.get("is_volume_muted")
            if not isinstance(muted, bool):
                return {}
            return {"on": muted}

        level = attributes.get("volume_level")
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            return {}
        if not 0 <= level <= 1:
            return {}
        minimum, maximum, step = _limits(service)
        value = minimum + round(level * (maximum - minimum))
        value = minimum + round((value - minimum) / step) * step
        return {"volume": min(maximum, value)}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)

        if service.service_id in _MUTE_SERVICE_IDS:
            muted = payload.get("on")
            if not isinstance(muted, bool):
                return
            await hass.services.async_call(
                "media_player",
                "volume_mute",
                {"entity_id": entity_ids[0], "is_volume_muted": muted},
                blocking=True,
            )
            return

        volume = _wire_int(payload.get("volume"))
        if volume is None:
            return
        minimum, maximum, step = _limits(service)
        if not minimum <= volume <= maximum or (volume - minimum) % step != 0:
            return
        await hass.services.async_call(
            "media_player",
            "volume_set",
            {
                "entity_id": entity_ids[0],
                "volume_level": (volume - minimum) / (maximum - minimum),
            },
            blocking=True,
        )


ADAPTER = MediaPlayerServiceAdapter()


def _limits(service: ServiceRef) -> tuple[int, int, int]:
    minimum = service.contract_value("min", 0)
    maximum = service.contract_value("max", 100)
    step = service.contract_value("step", 1)
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (minimum, maximum, step)
    ):
        raise ValueError("volume contract min, max, and step must be integers")
    if not 0 <= minimum < maximum <= 100 or step <= 0:
        raise ValueError("volume contract must be within 0..100 with a positive step")
    if (maximum - minimum) % step != 0:
        raise ValueError("volume contract step must divide its range")
    return minimum, maximum, step


def _wire_int(value: object) -> int | None:
    """Accept only wire-plausible numbers (int or integral float); reject bool/str."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
