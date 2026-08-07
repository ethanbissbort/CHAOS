using Chaos.Host.Abstractions;
using Chaos.Host.Supervisor.Runtime;

namespace Chaos.Host.Supervisor;

/// <summary>
/// Everything the supervisor knows about the backend, in one consistent read.
/// </summary>
/// <remarks>
/// This is what an operator debugging at 2am gets, and what the gateway can put
/// on a status page. Every field that is not known is null, and null means
/// "we do not know" — never a comforting zero.
/// </remarks>
public sealed record BackendSnapshot
{
    public required BackendStatus Status { get; init; }

    /// <summary>Why the status is what it is. Always populated.</summary>
    public required string StatusDetail { get; init; }

    /// <summary>Set only in terminal <see cref="BackendStatus.Failed"/>.</summary>
    public string? FailureReason { get; init; }

    /// <summary>Restarts since supervision started.</summary>
    public int RestartCount { get; init; }

    /// <summary>Failures since the last run that lasted long enough to count.</summary>
    public int ConsecutiveFailures { get; init; }

    /// <summary>Failures inside the circuit breaker's sliding window.</summary>
    public int FailuresInBreakerWindow { get; init; }

    /// <summary>PID of the live child, or null when nothing is running.</summary>
    public int? ProcessId { get; init; }

    /// <summary>Exit code of the last child that exited. Null if none has.</summary>
    public int? LastExitCode { get; init; }

    public DateTimeOffset? LastExitAt { get; init; }

    /// <summary>How the last run ended, in words.</summary>
    public string? LastExitDetail { get; init; }

    /// <summary>
    /// Tail of the child's stderr — the current child's while it runs, and the
    /// dead child's after it exits. This is where the Python traceback is.
    /// </summary>
    public IReadOnlyList<string> StandardErrorTail { get; init; } = [];

    /// <summary>When the backend last became healthy. Null if it never has.</summary>
    public DateTimeOffset? RunningSince { get; init; }

    /// <summary>Time since <see cref="RunningSince"/>, computed at snapshot time.</summary>
    public TimeSpan? Uptime { get; init; }

    /// <summary>Last time <c>/health</c> answered ok.</summary>
    public DateTimeOffset? LastHealthyAt { get; init; }

    /// <summary>Result of the most recent health probe, healthy or not.</summary>
    public string? LastHealthDetail { get; init; }

    /// <summary>Which layout was chosen. <c>Unresolved</c> means no launch has succeeded.</summary>
    public BackendRuntimeLayout RuntimeLayout { get; init; } = BackendRuntimeLayout.Unresolved;

    /// <summary>The interpreter and source layout in one line.</summary>
    public string? RuntimeDescription { get; init; }

    public Uri? HealthEndpoint { get; init; }

    /// <summary>Backoff currently being waited out, when the status is Restarting.</summary>
    public TimeSpan? NextRestartDelay { get; init; }

    public DateTimeOffset? NextRestartAt { get; init; }

    /// <summary>Where lifecycle events are going (Event Log, or ILogger only).</summary>
    public string EventSinkDescription { get; init; } = "unknown";
}

/// <summary>
/// The read side of the supervisor, for anything that wants more than the
/// five-state <see cref="BackendStatus"/>.
/// </summary>
public interface IBackendSupervisorDiagnostics
{
    BackendSnapshot Snapshot { get; }
}
