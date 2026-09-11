"""Strict public wire schemas. No positional/suit aliases are silently accepted."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

Version = int


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TenantCreate(StrictModel):
    tenant_id: StrictStr = Field(pattern=r"^[0-9]{1,32}$")
    note: StrictStr = Field(default="", max_length=300)
    round_version: StrictInt = Field(default=1, ge=1, le=2147483646)
    timeout_seconds: StrictInt = Field(default=180, ge=10, le=3600)


class TenantUpdate(StrictModel):
    note: StrictStr | None = Field(default=None, max_length=300)
    round_version: StrictInt | None = Field(default=None, ge=1, le=2147483646)
    timeout_seconds: StrictInt | None = Field(default=None, ge=10, le=3600)
    enabled: StrictBool | None = None


class CloseRequest(StrictModel):
    round_version: StrictInt = Field(ge=1, le=2147483646)


class Envelope(StrictModel):
    protocol_version: Literal[1]
    type: Literal["round.get", "round.join", "hand.submit", "round.end", "ping"]
    request_id: StrictStr = Field(
        min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"
    )
    tenant_id: StrictStr
    client_id: StrictStr
    round_version: StrictInt | None
    payload: dict


class EmptyPayload(StrictModel):
    pass


class JoinPayload(StrictModel):
    start_event_id: StrictStr = Field(
        min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"
    )
    sync_basis: Literal["initial_start", "end_then_start"]
    previous_round_version: StrictInt | None


class HandCard(StrictModel):
    rank: Literal["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
    suit: Literal["s", "h", "c", "d"]


class HandPayload(StrictModel):
    start_event_id: StrictStr | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    cards: list[HandCard] = Field(min_length=13, max_length=13)


PAYLOADS = {"round.join": JoinPayload, "hand.submit": HandPayload}


def parse_message(value):
    # bool equals integer 1 in Python; explicitly reject boolean protocol versions.
    if type(value.get("protocol_version")) is not int:
        raise ValueError("protocol_version必须为整数1")
    envelope = Envelope.model_validate(value)
    body = PAYLOADS.get(envelope.type, EmptyPayload).model_validate(envelope.payload)
    result = envelope.model_dump()
    result["payload"] = body.model_dump()
    if envelope.type == "hand.submit" and body.start_event_id is None:
        result["payload"].pop("start_event_id", None)
    return result
