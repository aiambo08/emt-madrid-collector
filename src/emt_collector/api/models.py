from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

UNKNOWN_ESTIMATE = 999_999  # sentinel returned by the API when there is no estimate


class Geometry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = "Point"
    coordinates: list[float] = Field(default_factory=list)

    @property
    def lon(self) -> float | None:
        return self.coordinates[0] if len(self.coordinates) >= 2 else None

    @property
    def lat(self) -> float | None:
        return self.coordinates[1] if len(self.coordinates) >= 2 else None


class Arrive(BaseModel):
    """One estimated arrival of a bus at a stop (`data[0].Arrive[]`)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    line: str
    stop: str
    bus: int
    destination: str | None = None
    is_head: bool | None = Field(default=None, alias="isHead")
    deviation: int | None = None
    estimate_arrive: int = Field(alias="estimateArrive")
    distance_bus: int | None = Field(default=None, alias="DistanceBus")
    position_type_bus: str | None = Field(default=None, alias="positionTypeBus")
    geometry: Geometry | None = None

    @field_validator("line", "stop", mode="before")
    @classmethod
    def _to_str(cls, value: object) -> str:
        return str(value)

    @field_validator("is_head", mode="before")
    @classmethod
    def _parse_bool(cls, value: object) -> bool | None:
        if value is None or isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "y", "yes"}

    @property
    def has_estimate(self) -> bool:
        return 0 <= self.estimate_arrive < UNKNOWN_ESTIMATE

    @property
    def has_position(self) -> bool:
        return self.geometry is not None and self.geometry.lat is not None


class ArrivalsData(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    arrive: list[Arrive] = Field(default_factory=list, alias="Arrive")


class ArrivalsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    code: str
    description: str = ""
    server_time: datetime | None = Field(default=None, alias="datetime")
    data: list[ArrivalsData] = Field(default_factory=list)

    @property
    def arrivals(self) -> list[Arrive]:
        return [a for block in self.data for a in block.arrive]


class LineInfo(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    line: str
    label: str
    name_a: str | None = Field(default=None, alias="nameA")
    name_b: str | None = Field(default=None, alias="nameB")
    group: str | None = None

    @field_validator("line", "label", mode="before")
    @classmethod
    def _to_str(cls, value: object) -> str:
        return str(value)


class StopInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    stop: str
    name: str | None = None
    geometry: Geometry | None = None

    @field_validator("stop", mode="before")
    @classmethod
    def _to_str(cls, value: object) -> str:
        return str(value)


class LineStops(BaseModel):
    line: str
    stops: list[StopInfo] = Field(default_factory=list)
