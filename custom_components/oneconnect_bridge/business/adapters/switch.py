"""Huawei `switch.on` adapter with configurable wire and climate control."""

from __future__ import annotations

import logging

from ..models import ServiceRef

_LOGGER = logging.getLogger(__name__)


class SwitchServiceAdapter:
    service_id = "switch"
    _domains = {"climate", "light", "switch", "fan", "media_player"}

    def validate_service(self, service: ServiceRef) -> None:
        if service.service_type not in {"switch", "binarySwitch"}:
            raise ValueError("switch requires st: switch or binarySwitch")
        unknown = set(dict(service.contract)) - {"on_type", "on_mode"}
        if unknown:
            raise ValueError(
                f"switch has unsupported contract keys: {', '.join(sorted(unknown))}"
            )
        if _on_type(service) not in {"int", "bool"}:
            raise ValueError("switch contract on_type must be int or bool")
        if not isinstance(_on_mode(service), str) or not _on_mode(service):
            raise ValueError("switch contract on_mode must be non-empty text")

    def validate_entities(self, entity_ids: tuple[str, ...]) -> None:
        if len(entity_ids) != 1:
            raise ValueError("switch currently requires exactly one HA entity")
        domain, separator, _object_id = entity_ids[0].partition(".")
        if not separator or domain not in self._domains:
            raise ValueError(
                "switch requires a climate, light, switch, fan, or media_player entity"
            )

    async def read_state(
        self, hass, service: ServiceRef, entity_ids: tuple[str, ...]
    ) -> dict[str, object]:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        state = hass.states.get(entity_ids[0])
        if state is None:
            return {}
        domain = entity_ids[0].partition(".")[0]
        if domain == "climate":
            if state.state in {"unknown", "unavailable", "off"}:
                is_on = False
            else:
                is_on = True
        elif domain == "media_player":
            if state.state in {"unknown", "unavailable"}:
                return {}
            # ``standby`` is also off; ``playing`` / ``paused`` / ``idle`` are on.
            is_on = state.state not in {"off", "standby"}
        elif state.state in {"on", "off"}:
            is_on = state.state == "on"
        else:
            return {}
        return {"on": is_on if _on_type(service) == "bool" else int(is_on)}

    async def apply_command(
        self,
        hass,
        service: ServiceRef,
        entity_ids: tuple[str, ...],
        payload: dict[str, object],
    ) -> None:
        self.validate_service(service)
        self.validate_entities(entity_ids)
        enabled = _parse_on_value(service, payload.get("on"))
        if enabled is None:
            return
        domain, _separator, _object_id = entity_ids[0].partition(".")
        if domain == "climate":
            state = hass.states.get(entity_ids[0])
            hvac_mode = "off" if not enabled else _on_mode(service)
            allowed = state.attributes.get("hvac_modes") if state else None
            if isinstance(allowed, list) and allowed and hvac_mode not in allowed:
                return
            try:
                await hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": entity_ids[0], "hvac_mode": hvac_mode},
                    blocking=True,
                )
            except Exception as error:  # noqa: BLE001 - a command must not kill the worker
                _LOGGER.warning(
                    "Climate switch command for %s failed: %s",
                    entity_ids[0],
                    error,
                )
            return
        try:
            await hass.services.async_call(
                domain,
                "turn_on" if enabled else "turn_off",
                {"entity_id": entity_ids[0]},
                blocking=True,
            )
        except Exception as error:  # noqa: BLE001 - a command must not kill the worker
            _LOGGER.warning(
                "Switch command for %s failed: %s",
                entity_ids[0],
                error,
            )


ADAPTER = SwitchServiceAdapter()


def _parse_on_value(service: ServiceRef, value: object) -> bool | None:
    if _on_type(service) == "int":
        if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1}:
            return None
        return bool(value)
    return value if isinstance(value, bool) else None


def _on_type(service: ServiceRef) -> str:
    return service.contract_value("on_type", "int")


def _on_mode(service: ServiceRef) -> str:
    return service.contract_value("on_mode", "auto")
