#if !CHAOS_HOST_ABSTRACTIONS_EXTERNAL

namespace Chaos.Host.Abstractions;

// ---------------------------------------------------------------------------
// MIRROR OF A CONTRACT OWNED BY THE GATEWAY (Chaos.Host/Abstractions/).
//
// This file exists so the supervisor builds, tests and ships before the
// gateway's copy lands, and so the Linux CI leg never depends on the ordering
// of two parallel work streams. It compiles ONLY when no external abstractions
// project is referenced (see Chaos.Host.Supervisor.csproj).
//
// If the gateway's shape differs from what is written here, the gateway wins
// and this file is deleted, not patched around.
// ---------------------------------------------------------------------------

/// <summary>
/// Lifecycle state of the Python platform behind the gateway.
/// </summary>
/// <remarks>
/// There is no "probably fine". <see cref="Running"/> is only ever reported
/// when <c>GET /health</c> answered, and <see cref="Failed"/> is terminal: the
/// supervisor has stopped retrying and an operator has to act.
/// </remarks>
public enum BackendStatus
{
    /// <summary>Not started, or stopped on request. Nothing is running.</summary>
    Stopped = 0,

    /// <summary>
    /// A process exists (or is about to) but <c>/health</c> has not answered.
    /// Also used while a running backend's health checks are failing but the
    /// supervisor has not yet given up on it — never claim health we do not have.
    /// </summary>
    Starting = 1,

    /// <summary>The backend answered <c>/health</c>. The only state that means "serving".</summary>
    Running = 2,

    /// <summary>The backend died or went unhealthy; a restart is scheduled or under way.</summary>
    Restarting = 3,

    /// <summary>
    /// Terminal. The circuit breaker tripped, the runtime could not be resolved,
    /// or shutdown found an unrecoverable condition. The supervisor is not
    /// retrying and will not recover without intervention.
    /// </summary>
    Failed = 4,
}

/// <summary>
/// Supervises the Python platform process that the gateway proxies to.
/// </summary>
public interface IBackendSupervisor
{
    /// <summary>Current lifecycle state. Never blocks.</summary>
    BackendStatus Status { get; }

    /// <summary>Start supervising. Idempotent.</summary>
    Task StartAsync(CancellationToken cancellationToken);

    /// <summary>Stop the backend and supervision. Idempotent.</summary>
    Task StopAsync(CancellationToken cancellationToken);
}

#endif
