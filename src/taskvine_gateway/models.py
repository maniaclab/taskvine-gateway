from pydantic import BaseModel, Field


class ScaleRequest(BaseModel):
    replicas: int = Field(ge=0, description="Desired number of vine_worker replicas")


class PoolStatus(BaseModel):
    username: str
    desired_replicas: int
    ready_replicas: int
    manager_host: str
    manager_port: int
