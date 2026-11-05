from __future__ import annotations

import logging
import secrets
from threading import RLock

from ..core.cloud_runtime import (
    CloudM2MClient,
    CloudRuntimeError,
    OnlineChildCommand,
)
from ..const import MAX_CHILD_DEVICES_PER_GATEWAY
from ..storage import GatewayStore
from .child_protocol import (
    ChildIdentity,
    ChildMaterial,
    ChildProtocolError,
    build_batch_bind_request,
    build_batch_delete_request,
    build_child_command_response,
    build_data_sync_request,
    build_dev_info_sync_request,
    build_room_update_request,
    build_status_sync_request,
    decode_child_command,
    material_from_storage,
    parse_batch_bind_response,
)
from .models import GatewayPlan


_LOGGER = logging.getLogger(__name__)


class ChildManager:
    """Register desired children through the already authenticated parent session."""

    def __init__(self, store: GatewayStore, plan: GatewayPlan) -> None:
        self.store = store
        self.plan = plan
        self._materials: dict[str, tuple[object, ChildIdentity, ChildMaterial]] = {}
        self._entity_bridge = None
        self._data_sequence = 0x2E
        self._lock = RLock()
        self._client: CloudM2MClient | None = None
        self._access_token: str | None = None
        self._pending_state: dict[str, dict[str, dict[str, object]]] = {}
        self._registration_errors: list[str] = []

    @property
    def registration_errors(self) -> tuple[str, ...]:
        """Return the per-child failures from the last registration pass."""

        with self._lock:
            return tuple(self._registration_errors)

    def set_entity_bridge(self, entity_bridge) -> None:
        """Attach the HA adapter used for initial state and commands."""

        self._entity_bridge = entity_bridge

    def register_on_session(
        self,
        client: CloudM2MClient,
        access_token: str,
        *,
        integration_version: str,
        hardware_version: str,
    ) -> bool:
        """Reconcile all declared children before parent heartbeat starts."""

        if len(self.plan.children) > MAX_CHILD_DEVICES_PER_GATEWAY:
            _LOGGER.error(
                "Gateway %s child mapping has %d children; maximum is %d",
                client.serial_number,
                len(self.plan.children),
                MAX_CHILD_DEVICES_PER_GATEWAY,
            )
            return False

        client.on_child_command = self.handle_cloud_command
        with self._lock:
            self._client = client
            self._access_token = access_token

        success = True
        with self._lock:
            self._registration_errors.clear()
        for child in self.plan.children:
            try:
                self._register_one(
                    client,
                    access_token,
                    child,
                    integration_version=integration_version,
                    hardware_version=hardware_version,
                )
            except Exception as error:  # noqa: BLE001 - one child must not kill parent
                success = False
                detail = f"{child.key}: {error}"
                with self._lock:
                    self._registration_errors.append(detail)
                _LOGGER.error(
                    "Child %s for gateway %s failed during registration: %s",
                    child.key,
                    client.serial_number,
                    error,
                )
        self.prune_removed_children(client, access_token, self.plan)
        return success

    def _register_one(
        self,
        client: CloudM2MClient,
        access_token: str,
        child,
        *,
        integration_version: str,
        hardware_version: str,
    ) -> None:
        stored_identity = self.store.read_json(f"children/{child.key}/identity.json")
        if (
            stored_identity is not None
            and stored_identity.get("product_id") != child.product.prod_id
        ):
            self._remove_stored_child(client, access_token, child.key)
            stored_identity = None

        material_path = f"children/{child.key}/material.json"
        if stored_identity is not None:
            identity = self._identity_from_storage(child.key, stored_identity)
            stored_material = self.store.read_json(material_path)
            if stored_material is not None:
                material = material_from_storage(stored_material)
                if (
                    material.serial_number != identity.serial_number
                    or material.udid != identity.udid
                ):
                    raise ChildProtocolError(
                        f"stored material for {child.key} does not match its identity"
                    )
                self._activate_child(
                    client,
                    access_token,
                    child,
                    identity,
                    material,
                    integration_version=integration_version,
                    hardware_version=hardware_version,
                )
                return
            self.store.remove_child(child.key)

        identity = self._new_identity(child)
        material: ChildMaterial | None = None
        try:
            response = client.request(
                "child_batch_bind",
                build_batch_bind_request(
                    access_token=access_token,
                    token=secrets.token_bytes(2),
                    identity=identity,
                    plan=child,
                    firmware_version=integration_version,
                    hardware_version=hardware_version,
                    software_version=integration_version,
                ),
            )
            material = parse_batch_bind_response(response, identity=identity)
            self._activate_child(
                client,
                access_token,
                child,
                identity,
                material,
                integration_version=integration_version,
                hardware_version=hardware_version,
            )
        except Exception:
            if material is not None:
                self._delete_cloud_child(
                    client, access_token, material, "child_batch_delete_failed_registration"
                )
            raise

        self.store.write_json(
            f"children/{child.key}/identity.json",
            self._identity_storage(identity),
        )
        self.store.write_json(
            material_path,
            material.as_storage(
                product_id=identity.product_id,
                sub_product_id=identity.sub_product_id,
            ),
        )

    def _activate_child(
        self,
        client: CloudM2MClient,
        access_token: str,
        child,
        identity: ChildIdentity,
        material: ChildMaterial,
        *,
        integration_version: str,
        hardware_version: str,
    ) -> None:
        """Publish identity and initial state for one already-bound child."""

        dev_info = client.request(
            "child_dev_info_sync",
            build_dev_info_sync_request(
                access_token=access_token,
                token=secrets.token_bytes(2),
                identity=identity,
                material=material,
                plan=child,
                firmware_version=integration_version,
                hardware_version=hardware_version,
                software_version=integration_version,
            ),
        )
        if dev_info.get("errcode") != 0:
            raise CloudRuntimeError(
                f"child {child.key} devInfoSync errcode {dev_info.get('errcode')}"
            )

        # Accept observed statusSync errcode 1000 because the SDK still registers the child.
        status = client.request(
            "child_status_sync",
            build_status_sync_request(
                access_token=access_token,
                token=secrets.token_bytes(2),
                material=material,
            ),
        )
        _LOGGER.info(
            "Child %s registered for gateway %s (devInfoSync=%s, statusSync=%s)",
            child.key,
            client.serial_number,
            dev_info.get("errcode"),
            status.get("errcode"),
        )
        if status.get("errcode") not in {0, 1000}:
            raise CloudRuntimeError(
                f"child {child.key} statusSync errcode {status.get('errcode')}"
            )
        with self._lock:
            self._materials[material.dev_id] = (child, identity, material)
        self._flush_pending_state(child.key, client)
        if child.room:
            try:
                room_response = client.request(
                    "child_room_updated",
                    build_room_update_request(
                        access_token=access_token,
                        token=secrets.token_bytes(2),
                        material=material,
                        room=child.room,
                    ),
                )
                if room_response.get("errcode") != 0:
                    _LOGGER.warning(
                        "Child %s room update returned %s",
                        child.key,
                        room_response.get("errcode"),
                    )
                else:
                    _LOGGER.debug(
                        "Child %s room update accepted room=%s",
                        child.key,
                        child.room,
                    )
            except Exception as error:  # noqa: BLE001 - room metadata must not kill child
                _LOGGER.warning(
                    "Child %s room update failed: %s",
                    child.key,
                    error,
                )
        if self._entity_bridge is not None:
            initial_state = self._entity_bridge.read_child_state(child)
            _LOGGER.debug(
                "Child %s initial HA state before dataSync: %s",
                child.key,
                initial_state,
            )
            for service_id, data in initial_state.items():
                try:
                    _LOGGER.debug(
                        "Child %s sending initial dataSync sid=%s data=%s",
                        child.key,
                        service_id,
                        data,
                    )

                    response = client.request(
                        f"child_data_sync_{service_id}",
                        build_data_sync_request(
                            access_token=access_token,
                            token=secrets.token_bytes(2),
                            child=child,
                            material=material,
                            service_id=service_id,
                            data=data,
                            sequence=self._next_data_sequence(),
                        ),
                    )
                    if response.get("errcode") != 0:
                        _LOGGER.warning(
                            "Child %s initial %s dataSync returned %s",
                            child.key,
                            service_id,
                            response.get("errcode"),
                        )
                    else:
                        _LOGGER.debug(
                            "Child %s initial dataSync accepted sid=%s",
                            child.key,
                            service_id,
                        )
                except Exception as error:  # noqa: BLE001 - state is best effort
                    _LOGGER.warning(
                        "Child %s initial %s state sync failed: %s",
                        child.key,
                        service_id,
                        error,
                    )

    def prune_removed_children(
        self,
        client: CloudM2MClient,
        access_token: str,
        desired_plan: GatewayPlan | None,
    ) -> None:
        """Delete old child entries absent from the newly loaded YAML plan."""

        desired_keys = (
            {child.key for child in desired_plan.children}
            if desired_plan is not None
            else set()
        )
        known_keys = {
            child.key for child in self.plan.children
        } | set(self.store.read_child_materials())
        for child_key in sorted(known_keys - desired_keys):
            self._remove_stored_child(
                client, access_token, child_key, "child_batch_delete_removed"
            )
            _LOGGER.info("Removed child %s from gateway storage", child_key)

    def handle_cloud_command(
        self, client: CloudM2MClient, message
    ) -> OnlineChildCommand | None:
        """Convert one child M2M command into HA work and an ACK."""

        try:
            command = decode_child_command(
                message, known_dev_ids=set(self._materials)
            )
        except ChildProtocolError as error:
            _LOGGER.warning("Unable to decode child cloud command: %s", error)
            return None
        if command is None:
            return None
        if client.online_access_token is None:
            return None
        child, _identity, _material = self._materials[command.dev_id]
        if self._entity_bridge is not None:
            def post_ack(ready_client: CloudM2MClient) -> None:
                try:
                    self._entity_bridge.apply_child_command(
                        child, command.service_id, command.payload
                    )
                    state = self._entity_bridge.read_child_state(child)
                    self.enqueue_state_snapshot(
                        child.key, state, client=ready_client
                    )
                except Exception as error:  # noqa: BLE001 - ACK already returned
                    _LOGGER.error(
                        "HA command handling failed for child %s: %s",
                        child.key,
                        error,
                    )
        else:
            post_ack = None
        response = build_child_command_response(
            command=command,
            access_token=client.online_access_token,
            sequence=client.next_child_response_sequence(),
        )
        _LOGGER.debug(
            "Child command ACK ready child=%s sid=%s",
            child.key,
            command.service_id,
        )
        return OnlineChildCommand(response=response, post_ack=post_ack)

    def enqueue_state_snapshot(
        self,
        child_key: str,
        state: dict[str, dict[str, object]],
        *,
        client: CloudM2MClient | None = None,
    ) -> bool:
        """Queue an HA-originated state snapshot on the parent cloud worker."""

        with self._lock:
            active_client = client or self._client
            access_token = self._access_token
            child = next((item for item in self.plan.children if item.key == child_key), None)
            material_record = next(
                (record for record in self._materials.values() if record[0].key == child_key),
                None,
            )
        if active_client is None or not access_token or child is None or material_record is None:
            with self._lock:
                self._pending_state[child_key] = state
            _LOGGER.debug(
                "Skipping HA state snapshot child=%s: cloud child session is not ready",
                child_key,
            )
            return False
        material = material_record[2]
        requests = []
        for service_id, data in state.items():
            requests.append(
                build_data_sync_request(
                    access_token=access_token,
                    token=secrets.token_bytes(2),
                    child=child,
                    material=material,
                    service_id=service_id,
                    data=data,
                    sequence=self._next_data_sequence(),
                )
            )
        active_client.enqueue_child_state_requests(requests)
        _LOGGER.debug(
            "Queued HA state snapshot child=%s services=%s",
            child_key,
            list(state),
        )
        return True

    def _flush_pending_state(self, child_key: str, client: CloudM2MClient) -> None:
        with self._lock:
            state = self._pending_state.pop(child_key, None)
        if state:
            self.enqueue_state_snapshot(child_key, state, client=client)

    def _next_data_sequence(self) -> bytes:
        value = bytes((self._data_sequence,))
        self._data_sequence = (self._data_sequence + 1) & 0xFF
        return value

    def _identity_from_storage(self, child_key: str, stored) -> ChildIdentity:
        try:
            identity = ChildIdentity(
                serial_number=str(stored["serial_number"]),
                udid=str(stored["udid"]),
                product_id=str(stored["product_id"]),
                sub_product_id=str(stored["sub_product_id"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ChildProtocolError(
                f"stored identity for {child_key} is incomplete"
            ) from error
        return identity

    @staticmethod
    def _new_identity(child) -> ChildIdentity:
        return ChildIdentity(
            serial_number=secrets.token_hex(12).upper(),
            udid=secrets.token_hex(32),
            product_id=child.product.prod_id,
            sub_product_id=child.product.sub_prod_id,
        )

    @staticmethod
    def _identity_storage(identity: ChildIdentity) -> dict[str, str]:
        return {
            "schema": "hwhomebridge-python.child-identity.v1",
            "product_id": identity.product_id,
            "sub_product_id": identity.sub_product_id,
            "serial_number": identity.serial_number,
            "udid": identity.udid,
        }

    def _remove_stored_child(
        self,
        client: CloudM2MClient,
        access_token: str,
        child_key: str,
        request_name: str = "child_batch_delete_replaced",
    ) -> None:
        try:
            stored_material = self.store.read_json(
                f"children/{child_key}/material.json"
            )
            if stored_material is not None:
                self._delete_cloud_child(
                    client,
                    access_token,
                    material_from_storage(stored_material),
                    request_name,
                )
        except Exception as error:  # noqa: BLE001 - local cleanup must continue
            _LOGGER.warning("Unable to delete old child %s from cloud: %s", child_key, error)
        finally:
            self.store.remove_child(child_key)
            with self._lock:
                for dev_id, record in tuple(self._materials.items()):
                    if record[0].key == child_key:
                        del self._materials[dev_id]
                self._pending_state.pop(child_key, None)
            _LOGGER.info("Discarded previous product state for child %s", child_key)

    @staticmethod
    def _delete_cloud_child(
        client: CloudM2MClient,
        access_token: str,
        material: ChildMaterial,
        request_name: str,
    ) -> None:
        try:
            response = client.request(
                request_name,
                build_batch_delete_request(
                    access_token=access_token,
                    token=secrets.token_bytes(2),
                    material=material,
                ),
            )
            if response.get("errcode") != 0:
                _LOGGER.warning(
                    "Cloud child delete devId=%s returned %s",
                    material.dev_id,
                    response.get("errcode"),
                )
        except Exception as error:  # noqa: BLE001 - caller owns local cleanup
            _LOGGER.warning("Cloud child delete devId=%s failed: %s", material.dev_id, error)
