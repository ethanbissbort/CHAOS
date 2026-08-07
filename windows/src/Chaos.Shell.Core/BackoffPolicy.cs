namespace Chaos.Shell.Core;

/// <summary>
/// Exponential backoff with bounded jitter, used both for waiting on the
/// gateway at startup and for reconnecting after the link drops.
/// </summary>
/// <remarks>
/// The jitter sample is passed in rather than drawn internally so the policy is
/// a pure function and can be tested exactly. The Windows layer supplies
/// <c>Random.Shared.NextDouble()</c>.
/// </remarks>
public sealed record BackoffPolicy
{
    /// <summary>
    /// Startup: probe quickly at first, because the usual case is the gateway
    /// coming up a second or two behind the shell, then ease off to two seconds
    /// so a long service start does not turn into a request flood.
    /// </summary>
    public static readonly BackoffPolicy Startup = new()
    {
        Initial = TimeSpan.FromMilliseconds(250),
        Maximum = TimeSpan.FromSeconds(2),
        Multiplier = 1.6,
        JitterRatio = 0.1,
    };

    /// <summary>
    /// Reconnect: the platform is expected to be up, so retry politely and back
    /// off to a fifteen-second ceiling. The tray already shows NOT CONNECTED,
    /// so hammering the gateway adds nothing an operator can see.
    /// </summary>
    public static readonly BackoffPolicy Reconnect = new()
    {
        Initial = TimeSpan.FromSeconds(1),
        Maximum = TimeSpan.FromSeconds(15),
        Multiplier = 2.0,
        JitterRatio = 0.2,
    };

    public TimeSpan Initial { get; init; } = TimeSpan.FromSeconds(1);

    public TimeSpan Maximum { get; init; } = TimeSpan.FromSeconds(30);

    public double Multiplier { get; init; } = 2.0;

    /// <summary>Fractional spread applied either side of the nominal delay.</summary>
    public double JitterRatio { get; init; }

    /// <summary>
    /// Delay before <paramref name="attempt"/>, which is 1-based: attempt 1 is
    /// the first retry after the first failure.
    /// </summary>
    /// <param name="jitterSample">
    /// A value in [0, 1). 0.5 gives the nominal delay with no jitter applied.
    /// </param>
    public TimeSpan DelayFor(int attempt, double jitterSample = 0.5)
    {
        if (attempt < 1)
        {
            throw new ArgumentOutOfRangeException(nameof(attempt), attempt, "Attempts are 1-based.");
        }

        if (jitterSample is < 0 or >= 1 || double.IsNaN(jitterSample))
        {
            throw new ArgumentOutOfRangeException(
                nameof(jitterSample), jitterSample, "The jitter sample must lie in [0, 1).");
        }

        var nominal = Initial.TotalMilliseconds * Math.Pow(Multiplier, attempt - 1);

        // Cap before jitter so the ceiling is a real ceiling: with jitter
        // applied afterwards the result can never exceed Maximum.
        nominal = Math.Min(nominal, Maximum.TotalMilliseconds);

        var spread = 1.0 + (JitterRatio * ((jitterSample * 2.0) - 1.0));
        var jittered = nominal * spread;

        // Guard against a pathological Multiplier or Initial producing NaN or
        // infinity: a broken delay must degrade to the ceiling, not to zero,
        // because a zero delay is a spin loop against the gateway.
        if (double.IsNaN(jittered) || double.IsInfinity(jittered))
        {
            return Maximum;
        }

        var clamped = Math.Clamp(jittered, 0.0, Maximum.TotalMilliseconds);
        return TimeSpan.FromMilliseconds(clamped);
    }
}
