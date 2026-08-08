namespace Chaos.Shell.Core;

/// <summary>
/// What the gateway said about the Python backend behind it.
/// </summary>
public enum BackendState
{
    /// <summary>The gateway did not say, or said something unrecognised.</summary>
    Unknown = 0,

    /// <summary>Serving.</summary>
    Up = 1,

    /// <summary>Coming up. The gateway is still inside its start window.</summary>
    Starting = 2,

    /// <summary>Not answering the gateway.</summary>
    Down = 3,
}

/// <summary>What one look at the gateway found.</summary>
/// <remarks>
/// A 503 from <c>/health</c> is still a reachable gateway. The gateway answers
/// 503 precisely when the backend behind it is down, and treating that as "no
/// gateway" would send an operator hunting for a process that is running fine
/// and hide the one that is not.
/// </remarks>
public sealed record GatewayProbe
{
    public static readonly GatewayProbe NotProbed = new();

    /// <summary>True once a probe has actually been made.</summary>
    public bool Attempted { get; init; }

    /// <summary>Something at the address answered HTTP.</summary>
    public bool Reachable { get; init; }

    /// <summary>The parsed body, when there was one.</summary>
    public PlatformHealth? Health { get; init; }

    /// <summary>HTTP status, when there was a response.</summary>
    public int? StatusCode { get; init; }

    /// <summary>Transport or protocol failure, verbatim.</summary>
    public string? Error { get; init; }

    /// <summary>Consecutive failed probes.</summary>
    public int ConsecutiveFailures { get; init; }

    /// <summary>When the gateway last answered, if it ever has.</summary>
    public DateTimeOffset? LastSuccessUtc { get; init; }

    /// <summary>The gateway's own version, when it reported one.</summary>
    public string? HostVersion { get; init; }

    /// <summary>
    /// What the gateway says about the backend. Unknown when the gateway itself
    /// is not reachable — the shell has no second opinion to fall back on.
    /// </summary>
    public BackendState Backend => Reachable ? (Health?.BackendState ?? BackendState.Unknown) : BackendState.Unknown;

    public static GatewayProbe Answered(
        PlatformHealth? health,
        int statusCode,
        DateTimeOffset atUtc,
        string? hostVersion = null) => new()
        {
            Attempted = true,
            Reachable = true,
            Health = health,
            StatusCode = statusCode,
            LastSuccessUtc = atUtc,
            HostVersion = hostVersion ?? health?.Version,
        };

    public static GatewayProbe Silent(
        string error,
        int consecutiveFailures,
        DateTimeOffset? lastSuccessUtc = null) => new()
        {
            Attempted = true,
            Reachable = false,
            Error = error,
            ConsecutiveFailures = consecutiveFailures,
            LastSuccessUtc = lastSuccessUtc,
        };
}

/// <summary>
/// Everything the launcher state machine reasons about, gathered by the Windows
/// half and handed over as plain data.
/// </summary>
/// <remarks>
/// Deliberately a record of facts with no behaviour. Nothing in here calls
/// Windows, opens a socket or reads a registry key, which is what makes the
/// launcher's whole decision surface testable on a machine that has none of
/// those things.
/// </remarks>
public sealed record LauncherFacts
{
    public required HostEndpoints Endpoints { get; init; }

    public required ShellSettings Settings { get; init; }

    public GatewayProbe Gateway { get; init; } = GatewayProbe.NotProbed;

    public ServiceProbe Service { get; init; } = ServiceProbe.NotChecked(ShellMessages.ServiceName);

    public HostExecutableProbe Executable { get; init; } = HostExecutableProbe.NotSearched;

    public SetupSnapshot Setup { get; init; } = SetupSnapshot.NotChecked;

    /// <summary>Whether this shell has a live child process it started.</summary>
    public bool ManagedChildRunning { get; init; }

    /// <summary>Whether this shell has administrator rights.</summary>
    public bool IsElevated { get; init; }

    /// <summary>
    /// Whether the configured gateway address is this machine. False means the
    /// shell can watch the platform but cannot start or stop it, and must say so
    /// rather than offering a Start button that could not possibly work.
    /// </summary>
    public bool AddressIsThisMachine { get; init; } = true;

    /// <summary>An operator-initiated action is running now.</summary>
    public bool ActionInProgress { get; init; }

    /// <summary>What that action is, for the progress line.</summary>
    public string? ActionInProgressMessage { get; init; }

    /// <summary>The result of the last action, shown until the next one.</summary>
    public string? LastActionMessage { get; init; }

    /// <summary>
    /// Whether elevation has already been established as required — set after
    /// Windows has actually refused, so the launcher stops advising and starts
    /// insisting.
    /// </summary>
    public bool ElevationRefused { get; init; }

    /// <summary>Anything else the operator has to be told: a rejected setting, a bad argument.</summary>
    public IReadOnlyList<string> Notices { get; init; } = Array.Empty<string>();

    /// <summary>
    /// Who owns the platform, decided from the facts rather than remembered.
    /// </summary>
    public PlatformRunMode RunMode
    {
        get
        {
            // A child this shell started answers for the gateway before
            // anything else does: if we are the parent process, that is the
            // fact the operator most needs, whatever else is installed.
            if (ManagedChildRunning)
            {
                return PlatformRunMode.ManagedByThisShell;
            }

            if (Gateway.Reachable)
            {
                return Service.IsRunning ? PlatformRunMode.WindowsService : PlatformRunMode.Foreign;
            }

            if (!Gateway.Attempted)
            {
                return PlatformRunMode.Unknown;
            }

            // Nothing answered. Only claim "not running" with a second piece of
            // evidence: an installed service that is positively stopped.
            return Service.State == PlatformServiceState.Stopped
                ? PlatformRunMode.NotRunning
                : PlatformRunMode.Unknown;
        }
    }
}
