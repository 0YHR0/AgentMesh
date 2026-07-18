from uuid import UUID


class AgentMeshError(Exception):
    """Base class for expected application and domain errors."""


class InvalidFeatureConfiguration(AgentMeshError):
    pass


class InvalidIdentityConfiguration(AgentMeshError):
    pass


class AuthenticationRequired(AgentMeshError):
    pass


class AuthenticationFailed(AgentMeshError):
    pass


class AuthorizationDenied(AgentMeshError):
    pass


class FeatureDisabled(AgentMeshError):
    def __init__(self, feature: str, profile: str) -> None:
        super().__init__(f"Feature '{feature}' is disabled by the '{profile}' profile")
        self.feature = feature
        self.profile = profile


class InvalidToolRequest(AgentMeshError):
    pass


class ToolInvocationFailed(AgentMeshError):
    pass


class ToolResultTooLarge(ToolInvocationFailed):
    def __init__(self, actual_bytes: int, max_bytes: int) -> None:
        super().__init__(f"Tool result is {actual_bytes} bytes; maximum is {max_bytes} bytes")
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes


class InvalidArtifact(AgentMeshError):
    pass


class ArtifactNotFound(AgentMeshError):
    pass


class ArtifactVersionNotFound(AgentMeshError):
    pass


class ArtifactTooLarge(AgentMeshError):
    def __init__(self, actual_bytes: int, max_bytes: int) -> None:
        super().__init__(f"Artifact content is {actual_bytes} bytes; maximum is {max_bytes} bytes")
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes


class ArtifactIntegrityMismatch(AgentMeshError):
    def __init__(self, expected_sha256: str, actual_sha256: str) -> None:
        super().__init__("Artifact content does not match expected_sha256")
        self.expected_sha256 = expected_sha256
        self.actual_sha256 = actual_sha256


class InvalidTaskInput(AgentMeshError):
    pass


class TaskNotFound(AgentMeshError):
    def __init__(self, task_id: UUID) -> None:
        super().__init__(f"Task {task_id} was not found")
        self.task_id = task_id


class HandoffNotFound(AgentMeshError):
    def __init__(self, handoff_id: UUID) -> None:
        super().__init__(f"Handoff {handoff_id} was not found")
        self.handoff_id = handoff_id


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
