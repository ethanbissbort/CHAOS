namespace Chaos.Shell.Core;

/// <summary>What the startup loop should do next.</summary>
public enum StartupAction
{
    /// <summary>Probe the gateway now.</summary>
    Probe = 0,

    /// <summary>Wait <see cref="StartupDecision.Delay"/>, then probe again.</summary>
    Wait = 1,

    /// <summary>The gateway answered. Show the console.</summary>
    Ready = 2,

    /// <summary>The budget is spent. Show the diagnostic, not a blank window.</summary>
    GiveUp = 3,
}

/// <summary>One step of the startup wait, with the text to show while it runs.</summary>
/// <param name="Action">What to do next.</param>
/// <param name="Delay">How long to wait when <paramref name="Action"/> is Wait.</param>
/// <param name="Progress">
/// Operator-facing progress line. Always names the address being probed and the
/// attempt number: "starting…" with no detail is what makes a hung launch
/// indistinguishable from a broken one.
/// </param>
/// <param name="Fraction">
/// Elapsed fraction of the budget, 0..1, for a determinate progress bar.
/// </param>
public sealed record StartupDecision(
    StartupAction Action,
    TimeSpan Delay,
    string Progress,
    double Fraction);

/// <summary>
/// The startup wait for the gateway, expressed as a pure decision function so
/// the whole policy — including the give-up boundary — is testable off Windows.
/// </summary>
public sealed class StartupSequence
{
    private readonly BackoffPolicy _backoff;
    private readonly Uri _probeTarget;

    public StartupSequence(Uri probeTarget, TimeSpan? budget = null, BackoffPolicy? backoff = null)
    {
        ArgumentNullException.ThrowIfNull(probeTarget);

        _probeTarget = probeTarget;
        _backoff = backoff ?? BackoffPolicy.Startup;

        // 45 s covers a cold Python start behind the gateway on a low-power
        // node. Past that the honest answer is "it is not coming up", with the
        // diagnostic that lets someone act on it.
        Budget = budget ?? TimeSpan.FromSeconds(45);

        if (Budget <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(budget), Budget, "The budget must be positive.");
        }
    }

    public TimeSpan Budget { get; }

    /// <summary>
    /// Decides the next step.
    /// </summary>
    /// <param name="attempt">Probes already made. 0 before the first.</param>
    /// <param name="elapsed">Time since the shell started waiting.</param>
    /// <param name="lastProbeSucceeded">Result of the most recent probe.</param>
    /// <param name="lastError">Why the most recent probe failed, if it did.</param>
    /// <param name="jitterSample">Jitter draw in [0, 1) for the backoff.</param>
    public StartupDecision Next(
        int attempt,
        TimeSpan elapsed,
        bool lastProbeSucceeded,
        string? lastError = null,
        double jitterSample = 0.5)
    {
        if (attempt < 0)
        {
            throw new ArgumentOutOfRangeException(nameof(attempt), attempt, "Attempts cannot be negative.");
        }

        if (elapsed < TimeSpan.Zero)
        {
            elapsed = TimeSpan.Zero;
        }

        var fraction = Math.Clamp(elapsed.TotalMilliseconds / Budget.TotalMilliseconds, 0.0, 1.0);

        if (lastProbeSucceeded)
        {
            return new StartupDecision(
                StartupAction.Ready,
                TimeSpan.Zero,
                $"Connected to {_probeTarget.Host}:{_probeTarget.Port}.",
                1.0);
        }

        if (attempt == 0)
        {
            return new StartupDecision(
                StartupAction.Probe,
                TimeSpan.Zero,
                $"Contacting the CHAOS gateway at {_probeTarget}…",
                fraction);
        }

        // The budget is checked against elapsed time only. Attempt count varies
        // with how fast probes fail, and an operator was promised a wait in
        // seconds, not in attempts.
        if (elapsed >= Budget)
        {
            return new StartupDecision(
                StartupAction.GiveUp,
                TimeSpan.Zero,
                $"The gateway at {_probeTarget} did not answer within "
                + $"{RelativeTime.Describe(Budget)} ({attempt} attempts).",
                1.0);
        }

        var delay = _backoff.DelayFor(attempt, jitterSample);

        // Never sleep past the deadline: the diagnostic should appear when it
        // was promised, not one backoff interval later.
        var remaining = Budget - elapsed;
        if (delay > remaining)
        {
            delay = remaining;
        }

        var because = string.IsNullOrWhiteSpace(lastError) ? string.Empty : $" — {lastError!.Trim()}";

        return new StartupDecision(
            StartupAction.Wait,
            delay,
            $"Waiting for the CHAOS gateway at {_probeTarget} "
            + $"(attempt {attempt}, {RelativeTime.Describe(elapsed)} elapsed){because}",
            fraction);
    }
}
