from pydantic import BaseModel


class AgentListItem(BaseModel, frozen=True):
    id: str
    name: str
    state: str


class AgentListResponse(BaseModel, frozen=True):
    agents: list[AgentListItem]


class SendMessageRequest(BaseModel, frozen=True):
    message: str


class SendMessageResponse(BaseModel, frozen=True):
    status: str


class ErrorResponse(BaseModel, frozen=True):
    detail: str
