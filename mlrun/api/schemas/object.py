from datetime import datetime
from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Extra


class ObjectMetadata(BaseModel):
    name: str
    project: Optional[str]
    tag: Optional[str]
    labels: Optional[dict]
    updated: Optional[datetime]
    uid: Optional[str]

    class Config:
        extra = Extra.allow


class ObjectStatus(BaseModel):
    state: Optional[str]

    class Config:
        extra = Extra.allow


class ObjectSpec(BaseModel):
    class Config:
        extra = Extra.allow


class LabelRecord(BaseModel):
    id: int
    name: str
    value: str

    class Config:
        orm_mode = True


class ObjectRecord(BaseModel):
    id: int
    name: str
    project: str
    uid: str
    updated: Optional[datetime] = None
    labels: List[LabelRecord]
    # state is extracted from the full status dict to enable queries
    state: Optional[str] = None
    full_object: Optional[dict] = None

    class Config:
        orm_mode = True


class ObjectKind(str, Enum):
    project = "project"
    feature_set = "FeatureSet"
    background_task = "BackgroundTask"
    feature_vector = "FeatureVector"
