"""Strict snake_case management contracts. Secrets never appear in responses."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ManagementBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("alias", "name", check_fields=False)
    @classmethod
    def nonblank(cls, value):
        if value is None or not value.strip():
            raise ValueError("A nonblank label is required")
        return value.strip()


class AutomationUpdate(ManagementBody):
    version: int = Field(ge=1)
    enabled: bool
    subscription_ids: list[str] = Field(max_length=500)

    @field_validator('subscription_ids')
    @classmethod
    def selected_ids(cls, values):
        if len(values) != len(set(values)) or any(not 1 <= len(v) <= 32 for v in values):
            raise ValueError('Unique configured subscription IDs required')
        return values


class AutomationRun(ManagementBody):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')


class DestinationCreate(ManagementBody):
    alias: str = Field(min_length=1, max_length=100)
    kind: Literal["test", "strategy", "radar", "ops"]


class DestinationUpdate(ManagementBody):
    version: int = Field(ge=1)
    alias: str | None = Field(default=None, min_length=1, max_length=100)
    kind: Literal["test", "strategy", "radar", "ops"] | None = None
    enabled: bool | None = None


class CredentialWrite(ManagementBody):
    version: int = Field(ge=1)
    webhook_url: str = Field(min_length=1, max_length=512, repr=False)
    signing_secret: str = Field(default="", max_length=512, repr=False)
    keyword: str = Field(default="阶跃", max_length=100)


class SubscriptionCreate(ManagementBody):
    name: str = Field(min_length=1, max_length=100)
    destination_id: str = Field(min_length=1, max_length=32)
    strategy_id: str = Field(min_length=1, max_length=100)
    top_n: int = Field(default=5, ge=1, le=20)
    llm_mode: Literal["off", "explain_only"] = "explain_only"
    planned_time: str = Field(default="17:10", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class SubscriptionUpdate(ManagementBody):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    destination_id: str | None = Field(default=None, min_length=1, max_length=32)
    strategy_id: str | None = Field(default=None, min_length=1, max_length=100)
    top_n: int | None = Field(default=None, ge=1, le=20)
    llm_mode: Literal["off", "explain_only"] | None = None
    planned_time: str | None = Field(default=None, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    enabled: bool | None = None


class SetupDestination(ManagementBody):
    alias: str = Field(min_length=1, max_length=100)
    webhook_url: str = Field(min_length=1, max_length=512, repr=False)
    signing_secret: str = Field(default="", max_length=512, repr=False)
    keyword: str = Field(default="阶跃", max_length=100)


class SetupCreate(SubscriptionCreate):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    destination_id: str | None = Field(default=None, min_length=1, max_length=32)
    destination_version: int | None = Field(default=None, ge=1)
    new_destination: SetupDestination | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def one_destination(self):
        if self.new_destination is not None:
            if self.destination_id is not None or self.destination_version is not None:
                raise ValueError("Choose one destination")
        elif self.destination_id is None or self.destination_version is None:
            raise ValueError("Existing destination and version required")
        return self


class PreviewRequest(ManagementBody):
    source_run_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    use_llm: bool = False


class PublicationAction(ManagementBody):
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class TestDeliveryRequest(ManagementBody):
    version: int = Field(ge=1)
    confirm: Literal[True]
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value
