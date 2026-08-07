namespace Chaos.Host.Abstractions;

/// <summary>
/// An immutable snapshot of what an <see cref="IBackendSupervisor"/> knows about
/// the Python platform backend.
/// </summary>
/// <remarks>
/// <para>
/// Every field other than <see cref="State"/> is optional and defaults to
/// <see langword="null"/> / zero. That is intentional: an implementation must be
/// able to say "I do not know" rather than filling a field with a plausible
/// value. The gateway renders a null as an explicit unknown on
/// <c>GET /host/info</c>; it never substitutes a default that reads as healthy.
/// </para>
/// <para>
/// Instances are expected to be cheap to produce — <see cref="IBackendSupervisor.Status"/>
/// is read on every <c>/health</c> and <c>/host/info</c> request and must not block.
/// </para>
/// </remarks>
public sealed record BackendStatus
{
    /// <summary>A status carrying no information. The default when no supervisor is registered.</summary>
    public static readonly BackendStatus Unknown = new();

    /// <summary>The supervisor's view of the backend lifecycle.</summary>
    public BackendState State { get; init; } = BackendState.Unknown;

    /// <summary>
    /// Short human-readable detail, safe to show an operator — for example
    /// "waiting for uvicorn to bind 127.0.0.1:8081". Null when there is nothing
    /// useful to add.
    /// </summary>
    public string? Detail { get; init; }

    /// <summary>
    /// The most recent error, if any. Populated for <see cref="BackendState.Faulted"/>
    /// and retained after a recovery so an operator can see what happened.
    /// </summary>
    public string? LastError { get; init; }

    /// <summary>When the current backend process started, or null if it is not running / unknown.</summary>
    public DateTimeOffset? StartedUtc { get; init; }

    /// <summary>Operating-system process id of the backend, or null if not running / unknown.</summary>
    public int? ProcessId { get; init; }

    /// <summary>Exit code of the last backend process, or null if it has not exited / is unknown.</summary>
    public int? LastExitCode { get; init; }

    /// <summary>How many times the supervisor has restarted the backend since the host started.</summary>
    public int RestartCount { get; init; }

    /// <summary>Creates a <see cref="BackendState.Starting"/> status.</summary>
    /// <param name="detail">Optional operator-facing detail.</param>
    /// <returns>The status.</returns>
    public static BackendStatus Starting(string? detail = null) =>
        new() { State = BackendState.Starting, Detail = detail };

    /// <summary>Creates a <see cref="BackendState.Running"/> status.</summary>
    /// <param name="processId">The backend process id, if known.</param>
    /// <param name="startedUtc">When the process started, if known.</param>
    /// <param name="restartCount">Restarts so far.</param>
    /// <returns>The status.</returns>
    public static BackendStatus Running(int? processId = null, DateTimeOffset? startedUtc = null, int restartCount = 0) =>
        new()
        {
            State = BackendState.Running,
            ProcessId = processId,
            StartedUtc = startedUtc,
            RestartCount = restartCount,
        };

    /// <summary>Creates a <see cref="BackendState.Stopped"/> status.</summary>
    /// <param name="lastExitCode">The exit code of the process that stopped, if known.</param>
    /// <returns>The status.</returns>
    public static BackendStatus Stopped(int? lastExitCode = null) =>
        new() { State = BackendState.Stopped, LastExitCode = lastExitCode };

    /// <summary>Creates a <see cref="BackendState.Faulted"/> status.</summary>
    /// <param name="error">Why the backend is faulted. Required — a fault with no reason is not actionable.</param>
    /// <param name="lastExitCode">The exit code, if the process exited.</param>
    /// <param name="restartCount">Restarts so far.</param>
    /// <returns>The status.</returns>
    public static BackendStatus Faulted(string error, int? lastExitCode = null, int restartCount = 0) =>
        new()
        {
            State = BackendState.Faulted,
            LastError = error,
            LastExitCode = lastExitCode,
            RestartCount = restartCount,
        };
}
