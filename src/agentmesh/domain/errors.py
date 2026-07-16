from uuid import UUID


class AgentMeshError(Exception):
    """Base class for expected application and domain errors."""


class InvalidTaskInput(AgentMeshError):
    pass


class TaskNotFound(AgentMeshError):
    def __init__(self, task_id: UUID) -> None:
        super().__init__(f"Task {task_id} was not found")
        self.task_id = task_id


class InvalidTaskTransition(AgentMeshError):
    pass


class ConcurrentTaskUpdate(AgentMeshError):
    pass


class TaskExecutionFailed(AgentMeshError):
    def __init__(self, task_id: UUID, message: str) -> None:
        super().__init__(message)
        self.task_id = task_id
