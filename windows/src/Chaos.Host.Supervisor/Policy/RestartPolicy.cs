using System.Globalization;

namespace Chaos.Host.Supervisor.Policy;

/// <summary>Jitter, injected so the policy is testable to the millisecond.</summary>
public interface IJitterSource
{
    /// <summary>A fraction in [0, 1).</summary>
    double NextFraction();
}

/// <summary>Real jitter.</summary>
public sealed class RandomJitterSource : IJitterSource
{
    public static RandomJitterSource Instance { get; } = new();

    public double NextFraction() => Random.Shared.NextDouble();
}

/// <summary>No jitter. Used by tests and by anyone who wants exact delays.</summary>
public sealed class NoJitterSource : IJitterSource
{
    public static NoJitterSource Instance { get; } = new();

    public double NextFraction() => 0.0;
}

/// <summary>What to do after a backend failure.</summary>
/// <param name="ShouldRetry">False means the circuit breaker tripped; the state is terminal.</param>
/// <param name="Delay">How long to wait before the next attempt.</param>
/// <param name="Reason">Operator-readable explanation of this decision.</param>
public readonly record struct RestartDecision(bool ShouldRetry, TimeSpan Delay, string Reason);

/// <summary>
/// Exponential backoff with a cap, plus a sliding-window circuit breaker.
/// </summary>
/// <remarks>
/// <para>
/// Two failure shapes need different answers. A backend that dies once because
/// a disk hiccupped should come straight back — that is what backoff is for.
/// A backend that cannot start at all (bad config, corrupt database, port
/// already bound) will fail identically forever, and retrying it every two
/// minutes until the heat death of the universe achieves nothing except a
/// status that reads "Starting" while the property has no control plane.
/// </para>
/// <para>
/// So after <see cref="BackendSupervisorOptions.MaxStartFailures"/> failures
/// inside <see cref="BackendSupervisorOptions.FailureWindow"/> the breaker
/// trips and the supervisor reports terminal <c>Failed</c> with a reason. An
/// honest hard failure is actionable; an infinite crash-loop dressed up as
/// "starting" is not.
/// </para>
/// <para>
/// The counter is forgiven once a backend has run healthily for
/// <see cref="BackendSupervisorOptions.HealthyRunDuration"/>, so a system that
/// crashes once a fortnight never accumulates its way into a false trip.
/// </para>
/// </remarks>
public sealed class RestartPolicy
{
    private readonly BackendSupervisorOptions _options;
    private readonly IJitterSource _jitter;
    private readonly Queue<DateTimeOffset> _failures = new();

    public RestartPolicy(BackendSupervisorOptions options)
        : this(options, RandomJitterSource.Instance)
    {
    }

    public RestartPolicy(BackendSupervisorOptions options, IJitterSource jitter)
    {
        _options = options ?? throw new ArgumentNullException(nameof(options));
        _jitter = jitter ?? throw new ArgumentNullException(nameof(jitter));
    }

    /// <summary>Failures since the last healthy run. Drives the backoff curve.</summary>
    public int ConsecutiveFailures { get; private set; }

    /// <summary>Failures still inside the sliding window. Drives the breaker.</summary>
    public int FailuresInWindow => _failures.Count;

    /// <summary>True once the breaker has tripped. Terminal until <see cref="Reset"/>.</summary>
    public bool IsTripped { get; private set; }

    /// <summary>
    /// Records a failure and decides what happens next.
    /// </summary>
    public RestartDecision RecordFailure(DateTimeOffset now)
    {
        ConsecutiveFailures++;
        _failures.Enqueue(now);
        Trim(now);

        if (_failures.Count >= _options.MaxStartFailures)
        {
            IsTripped = true;
            return new RestartDecision(
                false,
                TimeSpan.Zero,
                $"circuit breaker tripped: {_failures.Count.ToString(CultureInfo.InvariantCulture)} backend " +
                $"failures within {Describe(_options.FailureWindow)} " +
                $"(limit {_options.MaxStartFailures.ToString(CultureInfo.InvariantCulture)}). " +
                "The supervisor has stopped retrying; this needs a human.");
        }

        var delay = DelayFor(ConsecutiveFailures);
        return new RestartDecision(
            true,
            delay,
            $"restart {ConsecutiveFailures.ToString(CultureInfo.InvariantCulture)} in {Describe(delay)} " +
            $"({_failures.Count.ToString(CultureInfo.InvariantCulture)} of " +
            $"{_options.MaxStartFailures.ToString(CultureInfo.InvariantCulture)} failures in the " +
            $"{Describe(_options.FailureWindow)} window)");
    }

    /// <summary>
    /// Forgives the failure history after the backend stayed healthy long
    /// enough to count as a real run.
    /// </summary>
    public void RecordHealthyRun()
    {
        ConsecutiveFailures = 0;
        _failures.Clear();
    }

    /// <summary>Clears everything, including a tripped breaker.</summary>
    public void Reset()
    {
        RecordHealthyRun();
        IsTripped = false;
    }

    /// <summary>
    /// Backoff for the n-th consecutive failure: geometric, capped, then jittered
    /// upward by at most <see cref="BackendSupervisorOptions.BackoffJitterFraction"/>.
    /// </summary>
    public TimeSpan DelayFor(int consecutiveFailures)
    {
        if (consecutiveFailures < 1)
        {
            consecutiveFailures = 1;
        }

        var initial = _options.InitialRestartDelay.TotalMilliseconds;
        var cap = _options.MaxRestartDelay.TotalMilliseconds;

        var scaled = initial * Math.Pow(_options.BackoffMultiplier, consecutiveFailures - 1);
        if (double.IsNaN(scaled) || double.IsInfinity(scaled) || scaled > cap)
        {
            scaled = cap;
        }

        var jitter = scaled * _options.BackoffJitterFraction * _jitter.NextFraction();
        return TimeSpan.FromMilliseconds(scaled + jitter);
    }

    private void Trim(DateTimeOffset now)
    {
        while (_failures.Count > 0 && now - _failures.Peek() > _options.FailureWindow)
        {
            _failures.Dequeue();
        }
    }

    private static string Describe(TimeSpan value) =>
        value.TotalSeconds < 90
            ? value.TotalSeconds.ToString("0.#", CultureInfo.InvariantCulture) + "s"
            : value.TotalMinutes.ToString("0.#", CultureInfo.InvariantCulture) + "m";
}
