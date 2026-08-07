using Chaos.Shell.Core;

namespace Chaos.Shell.Core.Tests;

/// <summary>
/// The distinction this whole class defends: "the site is quiet" and "I cannot
/// see the site" are different answers and must never render the same way.
/// </summary>
public sealed class TrayStateFactoryTests
{
    private static readonly DateTimeOffset Now =
        new(2026, 8, 7, 12, 0, 0, TimeSpan.Zero);

    private static readonly AlarmCounts Quiet = AlarmCounts.None;

    private static readonly AlarmCounts TwoCritical = new()
    {
        Critical = 2,
        Unacknowledged = 2,
    };

    // ------------------------------------------------------------------------
    // The headline requirement.
    // ------------------------------------------------------------------------

    [Fact]
    public void Unreachable_backend_is_not_reported_as_no_alarms()
    {
        var offline = TrayStateFactory.Create(
            LinkStatus.Offline(Now.AddMinutes(-3), "connection refused", 12),
            Quiet,
            Now);

        var quiet = TrayStateFactory.Create(
            LinkStatus.Online(Now.AddSeconds(-2)),
            Quiet,
            Now);

        // Same alarm counts. Everything an operator can see must differ.
        Assert.NotEqual(quiet.Icon, offline.Icon);
        Assert.NotEqual(quiet.Tooltip, offline.Tooltip);
        Assert.NotEqual(quiet.BadgeKind, offline.BadgeKind);

        Assert.True(quiet.DataIsCurrent);
        Assert.False(offline.DataIsCurrent);

        // The offline tooltip must not contain a phrase that reads as "all clear".
        Assert.DoesNotContain("no active alarms", offline.Tooltip, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("NOT CONNECTED", offline.Tooltip, StringComparison.Ordinal);
        Assert.Contains("UNKNOWN", offline.Tooltip, StringComparison.Ordinal);
    }

    [Fact]
    public void Unreachable_badge_shows_unknown_never_zero()
    {
        var state = TrayStateFactory.Create(
            LinkStatus.Offline(Now.AddSeconds(-90), "timeout", 5),
            Quiet,
            Now);

        Assert.Equal(TrayBadgeKind.Unknown, state.BadgeKind);
        Assert.Equal("?", state.BadgeText);
        Assert.NotEqual("0", state.BadgeText);
    }

    [Fact]
    public void Unreachable_blinks_because_a_blind_shell_is_itself_a_condition()
    {
        var state = TrayStateFactory.Create(
            LinkStatus.Offline(Now.AddMinutes(-1), "connection refused", 8),
            Quiet,
            Now);

        Assert.True(state.Blink);
        Assert.Equal(TrayIconState.Unreachable, state.Icon);
    }

    [Fact]
    public void Losing_contact_while_alarms_were_active_still_reads_as_unknown()
    {
        // The dangerous middle case: two critical alarms were active, then the
        // link dropped. The panel must not keep asserting "2 critical" as if
        // current, and must not drop to "no alarms" either.
        var state = TrayStateFactory.Create(
            LinkStatus.Offline(Now.AddMinutes(-2), "socket closed", 20),
            TwoCritical,
            Now);

        Assert.Equal(TrayIconState.Unreachable, state.Icon);
        Assert.False(state.DataIsCurrent);
        Assert.Contains("UNKNOWN", state.Tooltip, StringComparison.Ordinal);

        // The last-known figure is still offered, explicitly labelled.
        Assert.Contains("Last known", state.SubHeadline, StringComparison.Ordinal);
        Assert.Contains("2 critical", state.SubHeadline, StringComparison.Ordinal);
    }

    // ------------------------------------------------------------------------
    // Never connected vs lost contact.
    // ------------------------------------------------------------------------

    [Fact]
    public void Never_connected_reads_as_connecting_not_offline()
    {
        var state = TrayStateFactory.Create(LinkStatus.Connecting(), Quiet, Now);

        Assert.Equal(TrayIconState.Starting, state.Icon);
        Assert.False(state.DataIsCurrent);
        Assert.Equal(TrayBadgeKind.Unknown, state.BadgeKind);
        Assert.Contains("No contact with the platform yet", state.SubHeadline, StringComparison.Ordinal);
    }

    [Fact]
    public void Offline_with_no_prior_contact_degrades_to_connecting()
    {
        // LinkStatus.Offline is handed a null last-contact by a shell that has
        // never succeeded. "Lost contact" would be a false claim.
        var state = TrayStateFactory.Create(
            LinkStatus.Offline(lastContactUtc: null, "connection refused", 3),
            Quiet,
            Now);

        Assert.Equal(TrayIconState.Starting, state.Icon);
        Assert.False(state.DataIsCurrent);
    }

    [Fact]
    public void Online_without_a_contact_timestamp_is_not_trusted()
    {
        // A self-contradictory link record must not produce a confident icon.
        var contradictory = new LinkStatus { Phase = LinkPhase.Online };

        var state = TrayStateFactory.Create(contradictory, Quiet, Now);

        Assert.Equal(TrayIconState.Starting, state.Icon);
        Assert.False(state.DataIsCurrent);
    }

    // ------------------------------------------------------------------------
    // Staleness.
    // ------------------------------------------------------------------------

    [Fact]
    public void Answer_older_than_the_freshness_budget_reads_as_stale()
    {
        var state = TrayStateFactory.Create(
            LinkStatus.Online(Now.AddSeconds(-45)),
            TwoCritical,
            Now);

        Assert.Equal(TrayIconState.Stale, state.Icon);
        Assert.False(state.DataIsCurrent);
        Assert.Contains("STALE", state.Tooltip, StringComparison.Ordinal);
        Assert.True(state.BadgeIsProvisional);
    }

    [Fact]
    public void Stale_with_no_alarms_still_does_not_claim_all_clear()
    {
        var state = TrayStateFactory.Create(
            LinkStatus.Online(Now.AddSeconds(-60)),
            Quiet,
            Now);

        Assert.Equal(TrayIconState.Stale, state.Icon);
        Assert.False(state.DataIsCurrent);
        Assert.Equal(TrayBadgeKind.Unknown, state.BadgeKind);
        Assert.Contains("Last known", state.Tooltip, StringComparison.Ordinal);
    }

    [Fact]
    public void Fresh_answer_just_inside_the_budget_is_current()
    {
        var options = TrayFreshnessOptions.Default;

        var state = TrayStateFactory.Create(
            LinkStatus.Online(Now - options.StaleAfter),
            Quiet,
            Now,
            options);

        // Exactly at the boundary is still current; past it is not.
        Assert.True(state.DataIsCurrent);
        Assert.Equal(TrayIconState.Normal, state.Icon);

        var justPast = TrayStateFactory.Create(
            LinkStatus.Online(Now - options.StaleAfter - TimeSpan.FromMilliseconds(1)),
            Quiet,
            Now,
            options);

        Assert.False(justPast.DataIsCurrent);
        Assert.Equal(TrayIconState.Stale, justPast.Icon);
    }

    // ------------------------------------------------------------------------
    // Severity mapping when the answer is current.
    // ------------------------------------------------------------------------

    [Theory]
    [InlineData(0, 0, 0, 0, TrayIconState.Normal)]
    [InlineData(0, 0, 0, 3, TrayIconState.Warning)]
    [InlineData(0, 0, 2, 0, TrayIconState.Alarm)]
    [InlineData(0, 1, 0, 0, TrayIconState.Alarm)]
    [InlineData(1, 0, 0, 0, TrayIconState.Emergency)]
    [InlineData(1, 5, 9, 9, TrayIconState.Emergency)]
    public void Worst_severity_drives_the_icon(
        int emergency, int critical, int major, int warning, TrayIconState expected)
    {
        var counts = new AlarmCounts
        {
            Emergency = emergency,
            Critical = critical,
            Major = major,
            Warning = warning,
            Unacknowledged = 0,
        };

        var state = TrayStateFactory.Create(LinkStatus.Online(Now.AddSeconds(-1)), counts, Now);

        Assert.Equal(expected, state.Icon);
        Assert.True(state.DataIsCurrent);
    }

    [Fact]
    public void Unrecognised_severity_escalates_rather_than_disappearing()
    {
        // A severity this build has never heard of must not be rounded down to
        // "nothing to see". It is counted and it raises the icon.
        var counts = AlarmCounts.FromSeverityMap(
            new Dictionary<string, int> { ["catastrophic"] = 1 });

        Assert.Equal(1, counts.Unclassified);
        Assert.Equal(1, counts.Total);

        var state = TrayStateFactory.Create(LinkStatus.Online(Now.AddSeconds(-1)), counts, Now);

        Assert.Equal(TrayIconState.Alarm, state.Icon);
        Assert.Equal("1", state.BadgeText);
    }

    [Fact]
    public void Quiet_site_says_so_plainly()
    {
        var state = TrayStateFactory.Create(LinkStatus.Online(Now.AddSeconds(-3)), Quiet, Now);

        Assert.Equal(TrayIconState.Normal, state.Icon);
        Assert.Equal(TrayBadgeKind.None, state.BadgeKind);
        Assert.Equal(string.Empty, state.BadgeText);
        Assert.False(state.Blink);
        Assert.True(state.DataIsCurrent);
        Assert.Contains("no active alarms", state.Tooltip, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("3 s ago", state.Tooltip, StringComparison.Ordinal);
    }

    // ------------------------------------------------------------------------
    // Badge and tooltip mechanics.
    // ------------------------------------------------------------------------

    [Theory]
    [InlineData(1, "1")]
    [InlineData(9, "9")]
    [InlineData(99, "99")]
    [InlineData(100, "99+")]
    [InlineData(4321, "99+")]
    public void Badge_saturates_rather_than_overflowing_the_icon(int total, string expected)
    {
        var counts = new AlarmCounts { Warning = total, Unacknowledged = 0 };

        var state = TrayStateFactory.Create(LinkStatus.Online(Now.AddSeconds(-1)), counts, Now);

        Assert.Equal(expected, state.BadgeText);
    }

    [Fact]
    public void Tooltip_never_exceeds_the_shell_limit()
    {
        var counts = new AlarmCounts
        {
            Emergency = 11,
            Critical = 22,
            Major = 33,
            Warning = 44,
            Unclassified = 55,
            Unacknowledged = 99,
        };

        var link = LinkStatus.Offline(
            Now.AddDays(-9),
            new string('e', 400),
            9999);

        foreach (var state in new[]
        {
            TrayStateFactory.Create(link, counts, Now),
            TrayStateFactory.Create(LinkStatus.Online(Now.AddSeconds(-1)), counts, Now),
            TrayStateFactory.Create(LinkStatus.Online(Now.AddHours(-5)), counts, Now),
            TrayStateFactory.Create(LinkStatus.Connecting(9999, new string('e', 400)), counts, Now),
        })
        {
            Assert.True(
                state.Tooltip.Length <= TrayState.TooltipMaxLength,
                $"tooltip was {state.Tooltip.Length} chars: {state.Tooltip}");
        }
    }

    [Fact]
    public void Truncated_tooltip_keeps_the_leading_state_words()
    {
        var text = TrayStateFactory.Clamp(new string('x', 500));

        Assert.Equal(TrayState.TooltipMaxLength, text.Length);
        Assert.EndsWith("…", text, StringComparison.Ordinal);
    }

    [Fact]
    public void Unacknowledged_critical_alarms_blink()
    {
        var blinking = TrayStateFactory.Create(
            LinkStatus.Online(Now.AddSeconds(-1)),
            new AlarmCounts { Critical = 1, Unacknowledged = 1 },
            Now);

        var acknowledged = TrayStateFactory.Create(
            LinkStatus.Online(Now.AddSeconds(-1)),
            new AlarmCounts { Critical = 1, Unacknowledged = 0 },
            Now);

        Assert.True(blinking.Blink);
        Assert.False(acknowledged.Blink);
    }

    [Fact]
    public void Unknown_acknowledgement_state_is_said_not_assumed()
    {
        var state = TrayStateFactory.Create(
            LinkStatus.Online(Now.AddSeconds(-1)),
            new AlarmCounts { Critical = 1, Unacknowledged = null },
            Now);

        Assert.Contains("acknowledgement state unknown", state.Tooltip, StringComparison.Ordinal);

        // Unknown is treated as possibly-unacknowledged, so it still blinks.
        Assert.True(state.Blink);
    }

    [Fact]
    public void Failure_detail_is_surfaced_for_the_menu()
    {
        var state = TrayStateFactory.Create(
            LinkStatus.Offline(Now.AddSeconds(-30), "No connection could be made", 4),
            Quiet,
            Now);

        Assert.Contains("4 failed attempts", state.SubHeadline, StringComparison.Ordinal);
        Assert.Contains("No connection could be made", state.SubHeadline, StringComparison.Ordinal);
    }

    [Fact]
    public void Clock_skew_does_not_produce_a_negative_age()
    {
        // A contact timestamp in the future (clock stepped by NTP) must not
        // render as "-4 s ago" or throw.
        var state = TrayStateFactory.Create(
            LinkStatus.Online(Now.AddSeconds(30)),
            Quiet,
            Now);

        Assert.Equal(TrayIconState.Normal, state.Icon);
        Assert.Contains("0 s ago", state.Tooltip, StringComparison.Ordinal);
    }
}
