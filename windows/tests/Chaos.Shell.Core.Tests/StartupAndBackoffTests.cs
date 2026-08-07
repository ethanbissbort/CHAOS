using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

public sealed class BackoffPolicyTests
{
    private static readonly BackoffPolicy Policy = new()
    {
        Initial = TimeSpan.FromSeconds(1),
        Maximum = TimeSpan.FromSeconds(16),
        Multiplier = 2.0,
        JitterRatio = 0.0,
    };

    [Theory]
    [InlineData(1, 1)]
    [InlineData(2, 2)]
    [InlineData(3, 4)]
    [InlineData(4, 8)]
    [InlineData(5, 16)]
    public void Delay_grows_geometrically(int attempt, double expectedSeconds)
    {
        Assert.Equal(expectedSeconds, Policy.DelayFor(attempt).TotalSeconds, precision: 6);
    }

    [Theory]
    [InlineData(6)]
    [InlineData(20)]
    [InlineData(1000)]
    public void Delay_is_capped_at_the_maximum(int attempt)
    {
        Assert.Equal(Policy.Maximum, Policy.DelayFor(attempt));
    }

    [Fact]
    public void Jitter_stays_within_the_declared_ratio_and_never_exceeds_the_cap()
    {
        var jittered = Policy with { JitterRatio = 0.25 };

        for (var attempt = 1; attempt <= 12; attempt++)
        {
            foreach (var sample in new[] { 0.0, 0.1, 0.5, 0.9, 0.999999 })
            {
                var delay = jittered.DelayFor(attempt, sample);
                var nominal = Math.Min(
                    Math.Pow(2, attempt - 1),
                    jittered.Maximum.TotalSeconds);

                Assert.InRange(
                    delay.TotalSeconds,
                    nominal * 0.75 - 1e-9,
                    Math.Min(nominal * 1.25, jittered.Maximum.TotalSeconds) + 1e-9);

                // The ceiling is a real ceiling even with jitter applied.
                Assert.True(delay <= jittered.Maximum);
            }
        }
    }

    [Fact]
    public void Midpoint_jitter_sample_gives_the_nominal_delay()
    {
        var jittered = Policy with { JitterRatio = 0.5 };

        Assert.Equal(4.0, jittered.DelayFor(3, 0.5).TotalSeconds, precision: 6);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(-1)]
    public void Attempts_are_one_based(int attempt)
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => Policy.DelayFor(attempt));
    }

    [Theory]
    [InlineData(-0.1)]
    [InlineData(1.0)]
    [InlineData(2.5)]
    [InlineData(double.NaN)]
    public void Jitter_sample_must_be_a_unit_fraction(double sample)
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => Policy.DelayFor(1, sample));
    }

    [Fact]
    public void A_degenerate_policy_degrades_to_the_ceiling_not_to_a_spin_loop()
    {
        // Overflowing the exponent must never produce a zero delay: that would
        // hammer the gateway as fast as the CPU allows.
        var absurd = new BackoffPolicy
        {
            Initial = TimeSpan.FromSeconds(1),
            Maximum = TimeSpan.FromSeconds(30),
            Multiplier = double.MaxValue,
        };

        Assert.Equal(absurd.Maximum, absurd.DelayFor(50));
    }

    [Fact]
    public void Shipped_policies_are_sane()
    {
        Assert.True(BackoffPolicy.Startup.Maximum <= TimeSpan.FromSeconds(5));
        Assert.True(BackoffPolicy.Startup.DelayFor(1) < TimeSpan.FromSeconds(1));
        Assert.True(BackoffPolicy.Reconnect.Maximum <= TimeSpan.FromSeconds(30));
        Assert.True(BackoffPolicy.Reconnect.DelayFor(1) >= TimeSpan.FromMilliseconds(500));
    }
}

/// <summary>
/// The startup wait. The failure mode being designed against is a shell that
/// sits on a blank window forever while an operator wonders whether the site is
/// running.
/// </summary>
public sealed class StartupSequenceTests
{
    private static readonly Uri Gateway = new("http://127.0.0.1:8080/health");

    private static StartupSequence Sequence(int budgetSeconds = 45) =>
        new(Gateway, TimeSpan.FromSeconds(budgetSeconds));

    [Fact]
    public void First_step_probes_immediately()
    {
        var decision = Sequence().Next(attempt: 0, TimeSpan.Zero, lastProbeSucceeded: false);

        Assert.Equal(StartupAction.Probe, decision.Action);
        Assert.Equal(TimeSpan.Zero, decision.Delay);
        Assert.Contains("127.0.0.1:8080", decision.Progress, StringComparison.Ordinal);
    }

    [Fact]
    public void A_successful_probe_ends_the_wait()
    {
        var decision = Sequence().Next(attempt: 3, TimeSpan.FromSeconds(4), lastProbeSucceeded: true);

        Assert.Equal(StartupAction.Ready, decision.Action);
        Assert.Equal(1.0, decision.Fraction);
    }

    [Fact]
    public void Success_is_honoured_even_past_the_budget()
    {
        // The gateway answering late still means it answered.
        var decision = Sequence(10).Next(attempt: 40, TimeSpan.FromSeconds(30), lastProbeSucceeded: true);

        Assert.Equal(StartupAction.Ready, decision.Action);
    }

    [Fact]
    public void Failure_inside_the_budget_waits_and_says_what_it_is_waiting_for()
    {
        var decision = Sequence().Next(
            attempt: 2,
            TimeSpan.FromSeconds(3),
            lastProbeSucceeded: false,
            lastError: "connection refused");

        Assert.Equal(StartupAction.Wait, decision.Action);
        Assert.True(decision.Delay > TimeSpan.Zero);

        // A progress line has to be specific enough to act on.
        Assert.Contains("attempt 2", decision.Progress, StringComparison.Ordinal);
        Assert.Contains("connection refused", decision.Progress, StringComparison.Ordinal);
        Assert.Contains(Gateway.ToString(), decision.Progress, StringComparison.Ordinal);
    }

    [Fact]
    public void The_wait_gives_up_only_after_the_budget_is_spent()
    {
        var sequence = Sequence(45);

        var justInside = sequence.Next(attempt: 9, TimeSpan.FromSeconds(44.9), lastProbeSucceeded: false);
        var justPast = sequence.Next(attempt: 9, TimeSpan.FromSeconds(45), lastProbeSucceeded: false);

        Assert.Equal(StartupAction.Wait, justInside.Action);
        Assert.Equal(StartupAction.GiveUp, justPast.Action);
        Assert.Contains("did not answer", justPast.Progress, StringComparison.Ordinal);
        Assert.Contains("9 attempts", justPast.Progress, StringComparison.Ordinal);
    }

    [Fact]
    public void The_wait_never_sleeps_past_its_own_deadline()
    {
        // Otherwise the diagnostic appears a whole backoff interval late, and
        // the operator stares at a spinner for longer than they were promised.
        var sequence = new StartupSequence(
            Gateway,
            TimeSpan.FromSeconds(10),
            new BackoffPolicy
            {
                Initial = TimeSpan.FromSeconds(8),
                Maximum = TimeSpan.FromSeconds(8),
                Multiplier = 1.0,
            });

        var decision = sequence.Next(attempt: 1, TimeSpan.FromSeconds(9.5), lastProbeSucceeded: false);

        Assert.Equal(StartupAction.Wait, decision.Action);
        Assert.True(
            decision.Delay <= TimeSpan.FromSeconds(0.5) + TimeSpan.FromMilliseconds(1),
            $"delay {decision.Delay} overshoots the deadline");
    }

    [Fact]
    public void Progress_fraction_is_bounded()
    {
        var sequence = Sequence(10);

        Assert.Equal(0.0, sequence.Next(0, TimeSpan.Zero, false).Fraction);
        Assert.Equal(0.5, sequence.Next(1, TimeSpan.FromSeconds(5), false).Fraction, precision: 6);
        Assert.Equal(1.0, sequence.Next(1, TimeSpan.FromSeconds(99), false).Fraction);
    }

    [Fact]
    public void Negative_elapsed_time_is_clamped_rather_than_throwing()
    {
        var decision = Sequence().Next(attempt: 1, TimeSpan.FromSeconds(-5), lastProbeSucceeded: false);

        Assert.Equal(StartupAction.Wait, decision.Action);
        Assert.Equal(0.0, decision.Fraction);
    }

    [Fact]
    public void A_non_positive_budget_is_rejected_at_construction()
    {
        Assert.Throws<ArgumentOutOfRangeException>(
            () => new StartupSequence(Gateway, TimeSpan.Zero));
    }

    [Fact]
    public void Negative_attempts_are_rejected()
    {
        Assert.Throws<ArgumentOutOfRangeException>(
            () => Sequence().Next(-1, TimeSpan.Zero, false));
    }
}

public sealed class RelativeTimeTests
{
    [Theory]
    [InlineData(0, "0 s")]
    [InlineData(3, "3 s")]
    [InlineData(59, "59 s")]
    [InlineData(60, "1 m")]
    [InlineData(3599, "59 m")]
    [InlineData(3600, "1 h 00 m")]
    [InlineData(3900, "1 h 05 m")]
    [InlineData(86400, "1 d 00 h")]
    [InlineData(360000, "4 d 04 h")]
    public void Ages_are_worded_compactly(int seconds, string expected)
    {
        Assert.Equal(expected, RelativeTime.Describe(TimeSpan.FromSeconds(seconds)));
    }

    [Fact]
    public void Ages_round_down_so_nothing_looks_fresher_than_it_is()
    {
        Assert.Equal("59 s", RelativeTime.Describe(TimeSpan.FromSeconds(59.99)));
        Assert.Equal("1 m", RelativeTime.Describe(TimeSpan.FromSeconds(119.99)));
    }

    [Fact]
    public void Negative_ages_clamp_to_zero()
    {
        Assert.Equal("0 s", RelativeTime.Describe(TimeSpan.FromSeconds(-30)));
    }

    [Fact]
    public void A_missing_timestamp_is_never()
    {
        Assert.Equal("never", RelativeTime.DescribeAge(null));
        Assert.Equal("5 s ago", RelativeTime.DescribeAge(TimeSpan.FromSeconds(5)));
    }
}
