namespace Chaos.Host.Abstractions;

/// <summary>
/// Lifecycle state of the Python platform backend as reported by an
/// <see cref="IBackendSupervisor"/>.
/// </summary>
/// <remarks>
/// This is the <em>supervisor's</em> view — "is the child process running" —
/// and is deliberately distinct from whether the backend is answering HTTP.
/// The gateway polls the backend independently and reports reachability
/// separately on <c>GET /health</c>. A process that is running but not yet
/// serving is a real and common state during startup, and the platform must
/// never collapse the two into a single reassuring "ok".
/// </remarks>
public enum BackendState
{
    /// <summary>
    /// Nothing is known about the backend. This is the honest initial value and
    /// the value reported when no supervisor is registered: it must never be
    /// interpreted as either healthy or failed.
    /// </summary>
    Unknown = 0,

    /// <summary>The supervisor has been asked to start the backend and the start has not completed.</summary>
    Starting = 1,

    /// <summary>The backend process is running as far as the supervisor can tell.</summary>
    Running = 2,

    /// <summary>The supervisor has been asked to stop the backend and the stop has not completed.</summary>
    Stopping = 3,

    /// <summary>The backend process is not running, by intent.</summary>
    Stopped = 4,

    /// <summary>
    /// The backend process exited unexpectedly, failed to start, or the
    /// supervisor cannot manage it. <see cref="BackendStatus.LastError"/>
    /// should say why.
    /// </summary>
    Faulted = 5,
}
