# Backend ownership and concurrency

**English** | [简体中文](backend-architecture.zh-CN.md)

Palette keeps the MCP, CLI, desktop IPC and configuration contracts stable while sharing
one application and one runtime owner. No data migration is required.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| `AgentService` | Typed business interface implemented by the local service and remote MCP proxy |
| `PaletteService` / `LifecycleService` | Observation and action orchestration; shared lifecycle authorization, instance identity and persistent VM routing |
| `DesktopManager` | Operator VM creation, settings and persistent lifecycle orchestration |
| `DefinitionRepository` / `PersistentStorage` | Atomic definition writes, disk ownership, expansion journals and recovery |
| `VmManager` / `VmStorage` | Processes, QMP sessions, startup rollback, disk references and retention |
| `ActionController` / `ActionScheduler` | Action transactions and their single per-instance queue; frame authority, deduplication, event planning and held-input records are separate components |
| `LocalListener` / `RequestRegistry` | Local connections and bounded operation ownership shared by the two authenticated IPC protocols |

## Concurrent operations

Lifecycle and definition mutations serialize per VM. A restart or reset owns the same
operation throughout stop, definition changes and subsequent startup. Nested application
steps reuse ownership in the same asyncio task. Other VMs can start, stop or change
settings independently, including while one VM waits for QEMU probing or shutdown.

Control changes acquire the VM operation before its control lock. Input executes through
the instance queue and never acquires lifecycle locks from that queue. Epoch changes fence
old frames and queued actions immediately. Lease expiry work proceeds independently per VM.

Shared disk reference updates and retention are synchronous transactions on the runtime
event loop. An async startup registers its backing image, volumes and persistent storage
before probing QEMU, so another VM's retention cannot delete them while no process exists
yet. References are released on success, failure and cancellation. This coordination does
not introduce cross-VM task placement or host-wide CPU/memory admission limits.

## Shutdown and failures

Desktop normal shutdown uses ACPI and reports failure without killing the guest. Existing
CLI shutdown fallback and explicit force-stop behavior remain available. Authorization and
expected-instance checks precede control changes; a stale stop cannot revoke a new lease.

Normal runtime exit refuses running processes and in-flight or queued starts. Full owner
shutdown closes start admission, cancels and joins starts and their rollback, closes IPC
and display connections, releases control, stops VMs and releases the owner lock. Shared
cleanup tasks finish even if their callers are cancelled repeatedly. Independent cleanup
steps are all attempted; the first failure is preserved.

IPC request replay remains bounded and process-local. A disconnected client does not
cancel the owned request. Conflicting authenticated Agent requests receive the existing
structured error format. Unknown or partial input outcomes are never automatically replayed.

## Verification

Concurrency tests use event barriers to hold one VM while asserting another can progress.
Regression tests cover persistent restart/reset, startup cancellation, retained backing
layers, stale stop requests, input ordering and cleanup failures. Fake QEMU exercises real
child processes and local IPC; it does not establish hardware acceptance. See the
[acceptance guide](acceptance-test.md) for explicitly configured real-QEMU checks.
