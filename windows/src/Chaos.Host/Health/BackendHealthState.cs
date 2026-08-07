namespace Chaos.Host.Health;

/// <summary>
/// The gateway's live view of whether the Python backend is reachable.
/// </summary>
/// <remarks>
/// <para>
/// Written by <see cref="BackendHealthMonitor"/>, read by <c>/health</c>,
/// <c>/host/info</c> and the proxy's error path. A single volatile reference to
/// an immutable snapshot, so readers never see a half-updated state and never block.
/// </para>
/// <para>
/// It starts as <see cref="BackendReachability.Starting"/> with no evidence of
/// anything, and only ever reports <see cref="BackendReachability.Up"/> after a
/// poll has actually succeeded.
/// </para>
/// </remarks>
public sealed class BackendHealthState
{
    private readonly DateTimeOffset _createdUtc = DateTimeOffset.UtcNow;
    private BackendHealthSnapshot _current = BackendHealthSnapshot.Initial;

    /// <summary>The current reading.</summary>
    public BackendHealthSnapshot Current => Volatile.Read(ref _current);

    /// <summary>When this state object was created — the gateway's start time.</summary>
    public DateTimeOffset CreatedUtc => _createdUtc;

    /// <summary>Records a successful poll.</summary>
    /// <param name="statusCode">The HTTP status returned.</param>
    /// <param name="backendVersion">The version the backend reported, or null if it did not.</param>
    /// <param name="versionCompatible">Whether that version satisfies the contract, or null if unknown.</param>
    /// <returns>Whether this reading changed reachability (used to decide whether to log).</returns>
    public bool RecordSuccess(int statusCode, string? backendVersion, bool? versionCompatible)
    {
        var previous = Current;
        var next = previous with
        {
            Reachability = BackendReachability.Up,
            LastCheckedUtc = DateTimeOffset.UtcNow,
            LastSuccessUtc = DateTimeOffset.UtcNow,
            LastError = null,
            LastStatusCode = statusCode,
            ConsecutiveFailures = 0,
            BackendVersion = backendVersion,
            VersionCompatible = versionCompatible,
        };

        Volatile.Write(ref _current, next);
        return previous.Reachability != BackendReachability.Up;
    }

    /// <summary>Records a failed poll.</summary>
    /// <param name="error">Why it failed. Recorded verbatim and surfaced to operators.</param>
    /// <param name="statusCode">The HTTP status, when the failure was a bad response rather than a transport error.</param>
    /// <param name="stillStarting">
    /// True while the start window has not expired, so the failure reports as
    /// <c>starting</c> rather than <c>down</c>.
    /// </param>
    /// <returns>Whether this reading changed reachability (used to decide whether to log).</returns>
    public bool RecordFailure(string error, int? statusCode, bool stillStarting)
    {
        var previous = Current;
        var reachability = stillStarting ? BackendReachability.Starting : BackendReachability.Down;
        var next = previous with
        {
            Reachability = reachability,
            LastCheckedUtc = DateTimeOffset.UtcNow,
            LastError = error,
            LastStatusCode = statusCode,
            ConsecutiveFailures = previous.ConsecutiveFailures + 1,
        };

        Volatile.Write(ref _current, next);
        return previous.Reachability != reachability;
    }
}
