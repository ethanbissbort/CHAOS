namespace Chaos.Shell.Core;

/// <summary>
/// Whether the shell is currently in contact with the CHAOS gateway.
/// </summary>
public enum LinkPhase
{
    /// <summary>Launched, no successful contact yet. Nothing is known.</summary>
    Connecting = 0,

    /// <summary>The last probe succeeded.</summary>
    Online = 1,

    /// <summary>The last probe failed. Anything previously read is now history.</summary>
    Offline = 2,
}

/// <summary>
/// The shell's view of its link to the gateway. Deliberately separate from
/// <see cref="AlarmCounts"/>: alarm counts describe the site, this describes
/// whether we are entitled to believe them.
/// </summary>
public sealed record LinkStatus
{
    public required LinkPhase Phase { get; init; }

    /// <summary>When the last successful probe completed, or null if never.</summary>
    public DateTimeOffset? LastContactUtc { get; init; }

    /// <summary>Failed probes since the last success.</summary>
    public int ConsecutiveFailures { get; init; }

    /// <summary>The last transport or HTTP error, verbatim, for diagnostics.</summary>
    public string? LastError { get; init; }

    /// <summary>The platform version reported by <c>/health</c>, when known.</summary>
    public string? PlatformVersion { get; init; }

    public static LinkStatus Connecting(int attempts = 0, string? lastError = null) => new()
    {
        Phase = LinkPhase.Connecting,
        ConsecutiveFailures = attempts,
        LastError = lastError,
    };

    public static LinkStatus Online(DateTimeOffset atUtc, string? platformVersion = null) => new()
    {
        Phase = LinkPhase.Online,
        LastContactUtc = atUtc,
        ConsecutiveFailures = 0,
        PlatformVersion = platformVersion,
    };

    public static LinkStatus Offline(
        DateTimeOffset? lastContactUtc,
        string? lastError,
        int consecutiveFailures) => new()
    {
        // Never having reached the gateway is Connecting, not Offline: the two
        // read differently to an operator and produce different tray text.
        Phase = lastContactUtc is null ? LinkPhase.Connecting : LinkPhase.Offline,
        LastContactUtc = lastContactUtc,
        LastError = lastError,
        ConsecutiveFailures = consecutiveFailures,
    };

    /// <summary>Age of the last successful contact, or null if there was none.</summary>
    public TimeSpan? AgeAt(DateTimeOffset nowUtc) =>
        LastContactUtc is { } last ? Max(nowUtc - last, TimeSpan.Zero) : null;

    private static TimeSpan Max(TimeSpan a, TimeSpan b) => a > b ? a : b;
}
