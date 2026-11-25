# Trusted worker executor extensions

`worker_executor_extension` is a default-off operator setting. It names an already
installed runtime extension, not a Python import path or task-provided module.
The extension must also be enabled in `runtime_extensions`, satisfy its feature
requirements, and be classified built-in, verified or local in the extension lock.

An extension can supply `RuntimeExtensionDefinition.worker_executor_factory`.
The factory receives an `ExtensionContext` and the normal version-bound fallback
executor. Its returned object must implement the existing AgentExecutor `execute`
contract. The worker currently exposes only its tenant-scoped unit-of-work factory
and model-connection service. API service factories are not started in the worker.

The normal executor remains the default when no extension is configured. A missing
factory, disabled/untrusted extension, absent feature or factory failure stops
startup; it never silently selects another execution engine.

This hook alone does not launch containers, grant Docker access, select an employee
runtime or change task inputs. A sandbox adapter must separately validate immutable
task/run/attempt and employee bindings, control credential delivery, persist dispatch
intent, enforce cancellation/deadlines, record provider usage and tool evidence, and
return results through the existing task finalization fences. Unknown outcomes must
not be converted into ordinary retryable failures or treated as proof of no charge.

Raise `ExternalExecutionUncertain` if an external request may have been accepted
but its final outcome cannot be verified. The legacy worker parks the Run as
`RECONCILIATION_REQUIRED` and its Attempt as `OUTCOME_UNKNOWN`. A coordinated
Subtask is parked without releasing dependent work. Budget and quota holds remain;
duplicate wakeups are acknowledged without another executor call. This signal is
not a reconciliation API: operators must inspect the external ledger before any
manual resolution, and adapters must not silently retry or switch to the fallback.

Enabling a Docker-backed broker is a separate operator deployment decision. Do not
give employee sandboxes a Docker socket or production filesystem mounts.
