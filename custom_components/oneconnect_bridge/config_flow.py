from __future__ import annotations

import copy
import logging
from pathlib import Path
import re
import secrets
import socket

import psutil
import voluptuous as vol
import yaml
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .business import load_enabled_mapping
from .const import CONF_INTERFACE_IP, CONF_MAC, CONF_SERIAL, DOMAIN
from .storage import GatewayStore


DEFAULT_MAPPING_PATH = Path(__file__).with_name("default_mapping.yaml")
_LOGGER = logging.getLogger(__name__)

# Huawei-provided HA profiles. The UI only exposes the device class and lets
# Home Assistant's entity picker fill the binding.
_PROFILES: dict[str, dict[str, object]] = {
    "light_9mu7": {
        "label": "智控灯泡（9MU7）",
        "domain": "light",
        "product": {
            "prod_id": "9MU7",
            "sub_prod_id": "00",
            "device_type": "119",
            "manufacturer": "002",
            "model": "haTest10",
            "protocol_type": 6,
            "setup_type": 0,
            "known_fields_only": False,
        },
        "services": [
            {"st": "switch", "sid": "switch"},
            {
                "st": "brightness",
                "sid": "brightness",
                "contract": {"min": 0, "max": 100, "step": 1},
            },
            {"st": "cct", "sid": "cct"},
        ],
    },
    "climate_9mxo": {
        "label": "智控空调（9MXO）",
        "domain": "climate",
        "product": {
            "prod_id": "9MXO",
            "sub_prod_id": "00",
            "device_type": "012",
            "manufacturer": "002",
            "model": "test20",
            "protocol_type": 6,
            "setup_type": 0,
            "known_fields_only": False,
        },
        "services": [
            {"st": "switch", "sid": "switch", "contract": {"on_mode": "cool"}},
            {
                "st": "mode",
                "sid": "mode",
                "contract": {
                    "values": {"cool": 2, "heat": 3, "fan_only": 4},
                },
            },
            {"st": "temperature", "sid": "temperature"},
            {
                "st": "fan",
                "sid": "fan",
                "contract": {
                    "gear_values": {
                        "auto": 0,
                        "low": 1,
                        "medium": 2,
                        "high": 3,
                    },
                    "direction_enabled": False,
                },
            },
        ],
    },
    "cover_9my5": {
        "label": "智控窗帘（9MY5）",
        "domain": "cover",
        "product": {
            "prod_id": "9MY5",
            "sub_prod_id": "00",
            "device_type": "01C",
            "manufacturer": "002",
            "model": "haTest23",
            "protocol_type": 6,
            "setup_type": 0,
            "known_fields_only": False,
        },
        "services": [
            {
                "st": "action",
                "sid": "leftAction",
                "contract": {"commands": {0: "close", 1: "open", 2: "stop"}},
            },
            {"st": "opener", "sid": "opener"},
        ],
    },
    "fan_9mxo": {
        "label": "风扇（9MXO 兼容模式，显示为空调）",
        "domain": "fan",
        "product": {
            "prod_id": "9MXO",
            "sub_prod_id": "00",
            "device_type": "012",
            "manufacturer": "002",
            "model": "test20",
            "protocol_type": 6,
            "setup_type": 0,
            "known_fields_only": False,
        },
        "services": [
            # HA-specific products use the default integer wire format (0/1);
            # using on_type: bool drops the cloud's numeric switch command.
            {"st": "switch", "sid": "switch"},
            {
                "st": "fan",
                "sid": "fan",
                "contract": {
                    "gear_percent": {1: 33, 2: 67, 3: 100},
                    "direction_enabled": False,
                },
            },
        ],
    },
    "fan_101z": {
        "label": "风扇（101z，档位 + 摇头）",
        "domain": "fan",
        "product": {
            "prod_id": "101z",
            "sub_prod_id": "00",
            "device_type": "029",
            "manufacturer": "005",
            "model": "未知",
            "protocol_type": 6,
            "setup_type": 0,
            "known_fields_only": False,
        },
        "services": [
            {"st": "switch", "sid": "binarySwitch"},
            {
                "st": "fan",
                "sid": "fan",
                "contract": {
                    "gear_enabled": False,
                    "speed_enabled": True,
                    "speed_min": 1,
                    "speed_max": 26,
                    "direction_enabled": False,
                    "angle_enabled": True,
                    "angle_on": 90,
                    "angle_off": 0,
                },
            },
        ],
    },
    "tv_9mu7": {
        "label": "电视电源（9MU7 兼容，仅开关）",
        "domain": "media_player",
        "product": {
            "prod_id": "9MU7",
            "sub_prod_id": "00",
            "device_type": "119",
            "manufacturer": "002",
            "model": "haTest10",
            "protocol_type": 6,
            "setup_type": 0,
            "known_fields_only": False,
        },
        "services": [
            {"st": "switch", "sid": "switch"},
        ],
    },

}


def _interface_options() -> dict[str, str]:
    options: dict[str, str] = {}
    for name, addresses in psutil.net_if_addrs().items():
        for address in addresses:
            if address.family != socket.AF_INET:
                continue
            if address.address.startswith("127."):
                continue
            options[address.address] = f"{name} — {address.address}"
    return options


def _interface_mac(interface_ip: str) -> str:
    for _name, addresses in psutil.net_if_addrs().items():
        ipv4 = {address.address for address in addresses if address.family == socket.AF_INET}
        if interface_ip not in ipv4:
            continue
        for address in addresses:
            if getattr(address.family, "name", "") in {"AF_LINK", "AF_PACKET"}:
                value = address.address.replace(":", "").replace("-", "").upper()
                if len(value) == 12:
                    return value
    raise ValueError(f"no MAC address found for interface {interface_ip}")


def _mapping_path(config_dir: str, serial: str) -> Path:
    store = GatewayStore(config_dir, serial)
    return store.ensure_mapping_file(DEFAULT_MAPPING_PATH)


def _read_mapping(path: Path) -> dict[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"mapping.yaml 无法读取：{error}") from error
    if not isinstance(value, dict):
        raise ValueError("mapping.yaml 必须是对象")
    children = value.get("children")
    if children is None:
        value["children"] = {}
    elif not isinstance(children, dict):
        raise ValueError("mapping.yaml 的 children 必须是对象")
    return value


def _write_mapping(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".flow")
    try:
        temporary.write_text(
            yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        load_enabled_mapping(temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _child_key(entity_id: str, children: dict[str, object]) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]", "_", entity_id.split(".", 1)[-1])
    base = base or "child"
    key = base
    index = 2
    while key in children:
        key = f"{base}_{index}"
        index += 1
    return key


def _build_child(
    profile_key: str, entity_id: str, name: str, room: str
) -> dict[str, object]:
    profile = _PROFILES[profile_key]
    product = copy.deepcopy(profile["product"])
    assert isinstance(product, dict)
    product["friendly_name"] = name
    if room:
        product["room"] = room
    services = copy.deepcopy(profile["services"])
    assert isinstance(services, list)
    product["services"] = services
    return {
        "product": product,
        "bindings": [
            {"sid": service["sid"], "entities": [entity_id]}
            for service in services
        ],
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create the HA entry and let the gateway pair asynchronously."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        options = await self.hass.async_add_executor_job(_interface_options)
        if not options:
            return self.async_abort(reason="no_ipv4_interface")

        errors: dict[str, str] = {}
        if user_input is not None:
            interface_ip = user_input[CONF_INTERFACE_IP]
            try:
                mac = await self.hass.async_add_executor_job(
                    _interface_mac, interface_ip
                )
            except ValueError:
                errors[CONF_INTERFACE_IP] = "mac_not_found"
            else:
                serial = secrets.token_hex(8).upper()
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=serial,
                    data={
                        CONF_INTERFACE_IP: interface_ip,
                        CONF_MAC: mac,
                        CONF_SERIAL: serial,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_INTERFACE_IP): vol.In(options)}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return OptionsFlow(config_entry)


class OptionsFlow(config_entries.OptionsFlow):
    """Manage the LAN interface and child-device mappings from the HA UI."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._profile_key: str | None = None
        self._entity_id: str | None = None

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            action = user_input["action"]
            if action == "interface":
                return await self.async_step_interface()
            if action == "add_child":
                return await self.async_step_add_child()
            if action == "remove_child":
                return await self.async_step_remove_child()
            if action == "clear_children":
                return await self.async_step_clear_children()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="interface"): vol.In(
                        {
                            "interface": "修改配对网卡",
                            "add_child": "添加子设备",
                            "remove_child": "删除子设备",
                            "clear_children": "清空失败子设备",
                        }
                    )
                }
            ),
        )

    async def async_step_interface(self, user_input=None):
        options = await self.hass.async_add_executor_job(_interface_options)
        if user_input is not None:
            interface_ip = user_input[CONF_INTERFACE_IP]
            mac = await self.hass.async_add_executor_job(_interface_mac, interface_ip)
            return self.async_create_entry(
                title="",
                data={CONF_INTERFACE_IP: interface_ip, CONF_MAC: mac},
            )
        return self.async_show_form(
            step_id="interface",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INTERFACE_IP,
                        default=self._entry.data.get(CONF_INTERFACE_IP),
                    ): vol.In(options)
                }
            ),
        )

    async def async_step_add_child(self, user_input=None):
        if user_input is not None:
            self._profile_key = user_input["profile"]
            return await self.async_step_add_child_entity()
        return self.async_show_form(
            step_id="add_child",
            data_schema=vol.Schema(
                {
                    vol.Required("profile"): vol.In(
                        {
                            key: str(profile["label"])
                            for key, profile in _PROFILES.items()
                        }
                    )
                }
            ),
        )

    async def async_step_add_child_entity(self, user_input=None):
        if self._profile_key is None:
            return self.async_abort(reason="unknown_action")
        profile = _PROFILES[self._profile_key]
        domain = str(profile["domain"])
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = user_input["entity_id"]
            if not entity_id.startswith(f"{domain}."):
                errors["base"] = "invalid_entity_domain"
            else:
                self._entity_id = entity_id
                return await self.async_step_add_child_details()
        return self.async_show_form(
            step_id="add_child_entity",
            data_schema=vol.Schema(
                {
                    vol.Required("entity_id"): EntitySelector(
                        EntitySelectorConfig(domain=domain)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_add_child_details(self, user_input=None):
        if self._profile_key is None or self._entity_id is None:
            return self.async_abort(reason="unknown_action")
        profile = _PROFILES[self._profile_key]
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input["name"]).strip()
            room = str(user_input.get("room", "")).strip()
            if not name:
                errors["base"] = "invalid_name"
            else:
                try:
                    await self.hass.async_add_executor_job(
                        self._save_child, name, room
                    )
                    await self._reload_mapping()
                except Exception as error:  # noqa: BLE001 - shown in the form
                    _LOGGER.exception(
                        "Unable to import child mapping for %s", self._entity_id
                    )
                    errors["base"] = "invalid_mapping"
                    placeholders["error"] = str(error)
                else:
                    return self.async_create_entry(
                        title="",
                        data=dict(self._entry.options),
                    )
        state = self.hass.states.get(self._entity_id)
        default_name = (
            state.attributes.get("friendly_name")
            if state is not None
            else str(profile["label"])
        )
        return self.async_show_form(
            step_id="add_child_details",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=default_name): str,
                    vol.Optional("room", default=""): str,
                }
            ),
            errors=errors,
            description_placeholders=placeholders or None,
        )

    async def async_step_remove_child(self, user_input=None):
        children = await self.hass.async_add_executor_job(self._child_keys)
        if not children:
            return self.async_abort(reason="no_children")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            key = user_input["child"]
            try:
                await self.hass.async_add_executor_job(self._remove_child, key)
                await self._reload_mapping()
            except Exception as error:  # noqa: BLE001 - shown in the form
                _LOGGER.exception("Unable to remove child mapping %s", key)
                errors["base"] = "invalid_mapping"
                placeholders["error"] = str(error)
            else:
                return self.async_create_entry(
                    title="",
                    data=dict(self._entry.options),
                )
        return self.async_show_form(
            step_id="remove_child",
            data_schema=vol.Schema({vol.Required("child"): vol.In(children)}),
            errors=errors,
            description_placeholders=placeholders or None,
        )

    async def async_step_clear_children(self, user_input=None):
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("confirm"):
                return self.async_abort(reason="cancelled")
            try:
                await self.hass.async_add_executor_job(self._clear_children)
                await self._reload_mapping()
            except Exception as error:  # noqa: BLE001 - shown in the form
                _LOGGER.exception("Unable to clear child mappings")
                errors["base"] = "invalid_mapping"
                placeholders["error"] = str(error)
            else:
                return self.async_create_entry(
                    title="",
                    data=dict(self._entry.options),
                )
        return self.async_show_form(
            step_id="clear_children",
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): bool}
            ),
            errors=errors,
            description_placeholders=placeholders or None,
        )

    def _save_child(self, name: str, room: str) -> None:
        assert self._profile_key is not None
        assert self._entity_id is not None
        serial = str(self._entry.data[CONF_SERIAL])
        path = _mapping_path(self.hass.config.path(), serial)
        mapping = _read_mapping(path)
        children = mapping["children"]
        assert isinstance(children, dict)
        key = _child_key(self._entity_id, children)
        children[key] = _build_child(self._profile_key, self._entity_id, name, room)
        _write_mapping(path, mapping)

    def _child_keys(self) -> dict[str, str]:
        serial = str(self._entry.data[CONF_SERIAL])
        path = _mapping_path(self.hass.config.path(), serial)
        mapping = _read_mapping(path)
        children = mapping["children"]
        assert isinstance(children, dict)
        result: dict[str, str] = {}
        for key, child in children.items():
            product = child.get("product") if isinstance(child, dict) else None
            name = product.get("friendly_name") if isinstance(product, dict) else None
            result[str(key)] = f"{name}（{key}）" if name else str(key)
        return result

    def _remove_child(self, key: str) -> None:
        serial = str(self._entry.data[CONF_SERIAL])
        path = _mapping_path(self.hass.config.path(), serial)
        mapping = _read_mapping(path)
        children = mapping["children"]
        assert isinstance(children, dict)
        if key not in children:
            raise ValueError(f"子设备不存在：{key}")
        children.pop(key)
        _write_mapping(path, mapping)

    def _clear_children(self) -> None:
        serial = str(self._entry.data[CONF_SERIAL])
        path = _mapping_path(self.hass.config.path(), serial)
        mapping = _read_mapping(path)
        children = mapping["children"]
        assert isinstance(children, dict)
        # Keep the optional link probe; remove every user-added child.
        mapping["children"] = {
            key: value
            for key, value in children.items()
            if key == "oneconnect_link_probe"
        }
        _write_mapping(path, mapping)

    async def _reload_mapping(self) -> None:
        callbacks = self.hass.data.get(DOMAIN, {}).get("reload_callbacks", {})
        reload_children = callbacks.get(self._entry.entry_id)
        if reload_children is not None:
            await reload_children()
