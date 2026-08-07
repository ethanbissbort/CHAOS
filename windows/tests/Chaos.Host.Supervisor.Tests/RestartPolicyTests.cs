using Chaos.Host.Supervisor.Policy;
using Xunit;

namespace Chaos.Host.Supervisor.Tests;

/// <summary>
/// The failure policy, tested as pure arithmetic: no clock, no tasks, no
/// sleeping. If this is wrong, a control system either crash-loops forever or
/// gives up on a backend that would have come back.
/// </summary>
public sealed class RestartPolicyTests
{
    private static BackendSupervisorOptions Options() => new()
    {
        InitialRestartDelay = TimeSpan.FromSeconds(1),
        MaxRestartDelay = TimeSpan.FromSeconds(10),
        BackoffMultiplier = 2.0,
        BackoffJitterFraction = 0.0,
        MaxStartFailures = 3,
        FailureWindow = TimeSpan.FromMinutes(5),
    };

    [Fact]
    public void Backoff_doubles_each_failure_and_stops_at_the_cap()
    {
        var policy = new RestartPolicy(Options(), NoJitterSource.Instance);

        Assert.Equal(TimeSpan.FromSeconds(1), policy.DelayFor(1));
        Assert.Equal(TimeSpan.FromSeconds(2), policy.DelayFor(2));
        Assert.Equal(TimeSpan.FromSeconds(4), policy.DelayFor(3));
        Assert.Equal(TimeSpan.FromSeconds(8), policy.DelayFor(4));
        Assert.Equal(TimeSpan.FromSeconds(10), policy.DelayFor(5));
        Assert.Equal(TimeSpan.FromSeconds(10), policy.DelayFor(6));
    }

    [Fact]
    public void Backoff_does_not_overflow_at_absurd_failure_counts()
    {
        var policy = new RestartPolicy(Options(), NoJitterSource.Instance);

        Assert.Equal(TimeSpan.FromSeconds(10), policy.DelayFor(2000));
    }

    [Fact]
    public void Jitter_only_ever_lengthens_the_delay_and_is_bounded()
    {
        var options = Options();
        options.BackoffJitterFraction = 0.25;
        var policy = new RestartPolicy(options, new FixedJitter(1.0));

        // Base 2s for the second failure, plus the full 25% jitter.
        Assert.Equal(TimeSpan.FromSeconds(2.5), policy.DelayFor(2));
    }

    [Fact]
    public void The_circuit_breaker_trips_on_the_configured_failure_count()
    {
        var policy = new RestartPolicy(Options(), NoJitterSource.Instance);
        var now = new DateTimeOffset(2026, 8, 7, 2, 0, 0, TimeSpan.Zero);

        var first = policy.RecordFailure(now);
        var second = policy.RecordFailure(now + TimeSpan.FromSeconds(5));
        var third = policy.RecordFailure(now + TimeSpan.FromSeconds(10));

        Assert.True(first.ShouldRetry);
        Assert.True(second.ShouldRetry);
        Assert.False(third.ShouldRetry);
        Assert.True(policy.IsTripped);
        Assert.Contains("circuit breaker tripped", third.Reason, StringComparison.Ordinal);
        Assert.Contains("needs a human", third.Reason, StringComparison.Ordinal);
    }

    [Fact]
    public void Failures_outside_the_window_do_not_accumulate_towards_a_trip()
    {
        var policy = new RestartPolicy(Options(), NoJitterSource.Instance);
        var now = new DateTimeOffset(2026, 8, 7, 2, 0, 0, TimeSpan.Zero);

        // Three failures, but spread far wider than the five-minute window: a
        // machine that drops the backend once an hour is not crash-looping.
        Assert.True(policy.RecordFailure(now).ShouldRetry);
        Assert.True(policy.RecordFailure(now + TimeSpan.FromHours(1)).ShouldRetry);
        Assert.True(policy.RecordFailure(now + TimeSpan.FromHours(2)).ShouldRetry);

        Assert.False(policy.IsTripped);
        Assert.Equal(1, policy.FailuresInWindow);
    }

    [Fact]
    public void A_healthy_run_forgives_the_history()
    {
        var policy = new RestartPolicy(Options(), NoJitterSource.Instance);
        var now = new DateTimeOffset(2026, 8, 7, 2, 0, 0, TimeSpan.Zero);

        policy.RecordFailure(now);
        policy.RecordFailure(now + TimeSpan.FromSeconds(1));
        Assert.Equal(2, policy.ConsecutiveFailures);

        policy.RecordHealthyRun();

        Assert.Equal(0, policy.ConsecutiveFailures);
        Assert.Equal(0, policy.FailuresInWindow);

        // And the next failure starts the backoff curve over.
        var decision = policy.RecordFailure(now + TimeSpan.FromMinutes(1));
        Assert.True(decision.ShouldRetry);
        Assert.Equal(TimeSpan.FromSeconds(1), decision.Delay);
    }

    [Fact]
    public void Consecutive_failures_drive_the_backoff_curve()
    {
        var policy = new RestartPolicy(Options(), NoJitterSource.Instance);
        var now = new DateTimeOffset(2026, 8, 7, 2, 0, 0, TimeSpan.Zero);

        Assert.Equal(TimeSpan.FromSeconds(1), policy.RecordFailure(now).Delay);
        Assert.Equal(TimeSpan.FromSeconds(2), policy.RecordFailure(now + TimeSpan.FromSeconds(1)).Delay);
    }

    [Fact]
    public void Reset_clears_a_tripped_breaker()
    {
        var policy = new RestartPolicy(Options(), NoJitterSource.Instance);
        var now = new DateTimeOffset(2026, 8, 7, 2, 0, 0, TimeSpan.Zero);

        policy.RecordFailure(now);
        policy.RecordFailure(now);
        policy.RecordFailure(now);
        Assert.True(policy.IsTripped);

        policy.Reset();

        Assert.False(policy.IsTripped);
        Assert.Equal(0, policy.FailuresInWindow);
    }

    private sealed class FixedJitter(double fraction) : IJitterSource
    {
        public double NextFraction() => fraction;
    }
}
