from uuid import UUID


class AgentMeshError(Exception):
    """Base class for expected application and domain errors."""


class InvalidFeatureConfiguration(AgentMeshError):
    pass


class FeatureDisabled(AgentMeshError):
    def __init__(self, feature: str, profile: str) -> None:
        super().__init__(f"Feature '{feature}' is disabled by the '{profile}' profile")
        self.feature = feature
        self.profile = profile


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


class RunLeaseUnavailable(AgentMeshError):
    pass


class InvalidMessage(AgentMeshError):
    pass


class IdempotencyConflict(AgentMeshError):
    pass


class AgentDefinitionNotFound(AgentMeshError):
    pass


class AgentVersionNotFound(AgentMeshError):
    pass


class CapabilityNotFound(AgentMeshError):
    pass


class AgentDeploymentNotFound(AgentMeshError):
    pass


class InvalidAgentDefinition(AgentMeshError):
    pass


class InvalidAgentVersion(AgentMeshError):
    pass


class InvalidAgentTransition(AgentMeshError):
    pass


class AgentRegistryConflict(AgentMeshError):
    pass


class AgentUnavailable(AgentMeshError):
    pass
