from datetime import datetime

from pydantic import BaseModel, model_validator
from typing import Any, Optional

class User(BaseModel):
    id: str
    _id: int
    phone: str
    last_name: str
    first_name: str
    middle_name: str

class House(BaseModel):
    id: str
    name: str
    iot_locked_id: Optional[str] = None
    address: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def parse_location(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("location"):
            addr = data["location"].get("address", {})
            parts = [addr.get("city"), addr.get("street"), addr.get("house")]
            data["address"] = ", ".join(filter(None, parts))
        return data

class Intercom(BaseModel):
    id: int
    intercom_id: int
    ble_allowed: bool
    is_ble: bool
    live_snapshot_url: Optional[str] = None
    name: str
    status: str
    webrtc_supported: bool
    sip_account_ex_user: Optional[int] = None
    sip_account_proxy: Optional[str] = None
    real_name: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def parse_visible_name(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("user_settings"):
            name = data["user_settings"].get("custom_name", None)
            if name is not None:
                data["real_name"] = data["name"]
                data["name"] = name
        return data

class DeviceState(BaseModel):
    status: str
    current_value: str
    units: str
    battery: Optional[float] = None
    error_code: Optional[str] = None

class Device(BaseModel):
    id: str
    natural_id: str
    type: str
    updated_at: Optional[datetime]
    name: Optional[str] = None
    room: Optional[str] = None
    last_activity: Optional[datetime]
    state: DeviceState

    @model_validator(mode="before")
    @classmethod
    def parse_last_activity(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("state"):
            last_activity = data["state"].get("last_activity", None)
            if last_activity is not None:
                data["last_activity"] = last_activity
        return data

    @model_validator(mode="before")
    @classmethod
    def parse_parameters(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("parameters"):
            name = data["parameters"].get("name", None)
            room = data["parameters"].get("room", None)
            if name is not None:
                data["name"] = name
            if room is not None:
                data["room"] = room
        return data

class Camera(BaseModel):
    id: str
    natural_id: int
    live_snapshot_url: Optional[str] = None
    rtsp_url: Optional[str] = None
    status: Optional[str] = None
    type: str
    name: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def parse_state(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("state"):
            live_snapshot_url = data["state"].get("live_snapshot_url", None)
            rtsp = data["state"].get("rtsp_url", None)
            status = data["state"].get("status", "offline")
            if live_snapshot_url is not None:
                data["live_snapshot_url"] = live_snapshot_url
            if rtsp is not None:
                data["rtsp_url"] = rtsp
            data["status"] = status
        return data

    @model_validator(mode="before")
    @classmethod
    def parse_name(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("parameters"):
            name = data["parameters"].get("name", None)
            if name is not None:
                data["name"] = name
        return data
