from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Node(StrictModel):
    name: str = Field(min_length=1,max_length=100)
    address: str = Field(min_length=1,max_length=250)

class ImageChoice(StrictModel):
    component: Literal['Dcu02Cpld','Dcu13Cpld','Dcu46Cpld','Dcu57Cpld']
    image_id: str = Field(min_length=1,max_length=60)
    expected: str = Field(default='',max_length=100)

class Draft(StrictModel):
    revision: str = Field(default='',max_length=80)
    mode: Literal['demo','live'] = 'live'
    targets: list[Node] = Field(min_length=1,max_length=2000)
    images: list[ImageChoice] = Field(default_factory=list,max_length=4)
    # Legacy workspace/API field; ignored for execution, never a required model.
    model: str = Field(default='',max_length=200,exclude=True)
    auth: Literal['basic','session'] = 'basic'
    self_signed: bool = False
    workers: int = Field(default=2,ge=1,le=4)
    canary_confirmed: bool = False

class JobRequest(StrictModel):
    action: Literal['read','power-read','asset-collect','precheck','verify']
    draft: Draft

class PlanRequest(StrictModel):
    action: Literal['upgrade','GracefulShutdown','ForceOff','On']
    draft: Draft

class ConfirmRequest(StrictModel):
    phrase: str = Field(max_length=100)

class TextRequest(StrictModel):
    text: str = Field(max_length=2_000_000)

class LoginRequest(StrictModel):
    key: str = Field(min_length=1,max_length=200)
    bootstrap: bool = False

class WorkspaceImage(StrictModel):
    component: Literal['Dcu02Cpld','Dcu13Cpld','Dcu46Cpld','Dcu57Cpld']
    sha256: str = Field(min_length=64,max_length=64)
    filename: str = Field(max_length=160)

class Workspace(StrictModel):
    format: Literal['bmc-web-workspace-v1']
    draft: Draft
    images: list[WorkspaceImage] = Field(max_length=4)


class AssetMetadata(StrictModel):
    declared_sn: str = Field(default='',max_length=200)
    declared_model: str = Field(default='',max_length=200)
    location: str = Field(default='',max_length=200)
    tags: str = Field(default='',max_length=300)
    notes: str = Field(default='',max_length=1000)
    lifecycle: Literal['active','spare','repair','retired'] = 'active'

class AssetEdit(AssetMetadata):
    mode: Literal['demo','live']
    address: str = Field(min_length=1,max_length=250)

class AssetRegister(StrictModel):
    mode: Literal['demo','live'] = 'live'
    targets: list[Node] = Field(min_length=1,max_length=20000)
