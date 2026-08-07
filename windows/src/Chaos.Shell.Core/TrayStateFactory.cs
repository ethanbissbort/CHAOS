using System.Globalization;

namespace Chaos.Shell.Core;

/// <summary>Freshness thresholds for <see cref="TrayStateFactory"/>.</summary>
public sealed record TrayFreshnessOptions
{
    public static readonly TrayFreshnessOptions Default = new();

    /// <summary>How often the shell polls the platform.</summary>
    public TimeSpan PollInterval { get; init; } = TimeSpan.FromSeconds(5);

    /// <summary>
    /// Beyond this age a successful answer is no longer presented as current.
    /// Four missed polls: long enough not to flicker on one slow request, short
    /// enough that a wall display cannot sit on a dead reading unnoticed.
    /// </summary>
    public TimeSpan StaleAfter { get; init; } = TimeSpan.FromSeconds(20);
}

/// <summary>
/// Turns "what the platform last said" plus "whether we can still see the
/// platform" into the tray presentation.
/// </summary>
/// <remarks>
/// <para>The rule this whole class exists to enforce:</para>
/// <para>
/// An unreachable platform never renders as a quiet one. A zero count is only
/// ever spoken aloud when the shell has a current answer that actually said
/// zero. Otherwise the shell says the alarm state is unknown, and says why.
/// </para>
/// </remarks>
public static class TrayStateFactory
{
    private const string Product = "Project CHAOS";

    public static TrayState Create(
        LinkStatus link,
        AlarmCounts counts,
        DateTimeOffset nowUtc,
        TrayFreshnessOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(link);
        ArgumentNullException.ThrowIfNull(counts);

        options ??= TrayFreshnessOptions.Default;
        var age = link.AgeAt(nowUtc);

        // An "Online" link with no contact timestamp is self-contradictory.
        // Treat it as never-contacted rather than trusting the phase.
        var phase = link.Phase == LinkPhase.Online && age is null
            ? LinkPhase.Connecting
            : link.Phase;

        return phase switch
        {
            LinkPhase.Connecting => Connecting(link, age, counts),
            LinkPhase.Offline => Unreachable(link, age, counts),
            _ when age > options.StaleAfter => Stale(link, age!.Value, counts),
            _ => Current(counts, age ?? TimeSpan.Zero),
        };
    }

    // ---------------------------------------------------------------- unknown --

    private static TrayState Connecting(LinkStatus link, TimeSpan? age, AlarmCounts counts)
    {
        // Reconnecting after a previous success is a different sentence from
        // never having connected at all, and the operator needs to tell them
        // apart. Both are "unknown"; only one has a last-known value.
        var reconnecting = age is not null;

        var sub = reconnecting
            ? $"Last contact {RelativeTime.DescribeAge(age)}. {LastKnown(counts)}"
            : "No contact with the platform yet.";

        return new TrayState
        {
            Icon = TrayIconState.Starting,
            Headline = $"{Product} — connecting",
            SubHeadline = Append(sub, FailureNote(link)),
            Tooltip = Clamp($"{Product} — connecting to the gateway. Alarm state UNKNOWN."),
            BadgeKind = TrayBadgeKind.Unknown,
            BadgeText = "?",
            BadgeIsProvisional = true,
            Blink = false,
            DataIsCurrent = false,
        };
    }

    private static TrayState Unreachable(LinkStatus link, TimeSpan? age, AlarmCounts counts)
    {
        // A control system you cannot see is itself a condition worth an
        // operator's attention, so this state blinks even though the shell has
        // no idea whether anything is wrong on site.
        var ageText = RelativeTime.DescribeAge(age);

        return new TrayState
        {
            Icon = TrayIconState.Unreachable,
            Headline = $"{Product} — NOT CONNECTED",
            SubHeadline = Append(
                $"Alarm state unknown. Last contact {ageText}. {LastKnown(counts)}",
                FailureNote(link)),
            Tooltip = Clamp($"{Product} — NOT CONNECTED. Alarm state UNKNOWN (last contact {ageText})."),
            BadgeKind = TrayBadgeKind.Unknown,
            BadgeText = "?",
            BadgeIsProvisional = true,
            Blink = true,
            DataIsCurrent = false,
        };
    }

    private static TrayState Stale(LinkStatus link, TimeSpan age, AlarmCounts counts)
    {
        _ = link;
        var ageText = RelativeTime.Describe(age);

        return new TrayState
        {
            Icon = TrayIconState.Stale,
            Headline = $"{Product} — STALE",
            SubHeadline = $"No update for {ageText}. {LastKnown(counts)}",
            Tooltip = Clamp($"{Product} — STALE, no update for {ageText}. {LastKnown(counts)}"),

            // The count is still shown, because "2 alarms as of a minute ago"
            // is more use than nothing — but it is flagged provisional so the
            // renderer can dim it, and the word STALE leads the tooltip.
            BadgeKind = counts.Any ? TrayBadgeKind.Count : TrayBadgeKind.Unknown,
            BadgeText = counts.Any ? BadgeFor(counts.Total) : "?",
            BadgeIsProvisional = true,
            Blink = false,
            DataIsCurrent = false,
        };
    }

    // ---------------------------------------------------------------- current --

    private static TrayState Current(AlarmCounts counts, TimeSpan age)
    {
        var ageText = RelativeTime.DescribeAge(age);

        if (!counts.Any)
        {
            return new TrayState
            {
                Icon = TrayIconState.Normal,
                Headline = $"{Product} — no active alarms",
                SubHeadline = $"Connected. Updated {ageText}.",
                Tooltip = Clamp($"{Product} — no active alarms. Updated {ageText}."),
                BadgeKind = TrayBadgeKind.None,
                BadgeText = string.Empty,
                DataIsCurrent = true,
            };
        }

        var icon = IconFor(counts);
        var summary = counts.Describe();
        var unack = counts.Unacknowledged;

        // Unacknowledged emergency and critical alarms are what a horn is for.
        var blink = unack is null or > 0
            && counts.Worst is AlarmSeverity.Emergency or AlarmSeverity.Critical;

        var unackText = unack is { } n
            ? $"{n} unacknowledged"
            : "acknowledgement state unknown";

        return new TrayState
        {
            Icon = icon,
            Headline = $"{Product} — {counts.Total} active alarm{(counts.Total == 1 ? string.Empty : "s")}",
            SubHeadline = $"{summary}. {unackText}. Updated {ageText}.",
            Tooltip = Clamp($"{Product} — {summary}. {unackText}. Updated {ageText}."),
            BadgeKind = TrayBadgeKind.Count,
            BadgeText = BadgeFor(counts.Total),
            BadgeIsProvisional = false,
            Blink = blink,
            DataIsCurrent = true,
        };
    }

    private static TrayIconState IconFor(AlarmCounts counts)
    {
        if (counts.Worst is AlarmSeverity.Emergency)
        {
            return TrayIconState.Emergency;
        }

        // A severity string the shell does not recognise is escalated to Alarm
        // rather than ignored or guessed downward. New severities must arrive
        // loudly, not quietly.
        if (counts.HasUnclassified)
        {
            return TrayIconState.Alarm;
        }

        return counts.Worst switch
        {
            AlarmSeverity.Critical or AlarmSeverity.Major => TrayIconState.Alarm,
            AlarmSeverity.Warning => TrayIconState.Warning,
            _ => TrayIconState.Normal,
        };
    }

    // ----------------------------------------------------------------- shared --

    /// <summary>
    /// Wording for counts that are no longer current. Always says "last known";
    /// never states a bare number that could be read as live.
    /// </summary>
    private static string LastKnown(AlarmCounts counts) =>
        counts.Any
            ? $"Last known: {counts.Describe()}."
            : "Last known: no active alarms.";

    private static string FailureNote(LinkStatus link)
    {
        if (link.ConsecutiveFailures <= 0 && string.IsNullOrWhiteSpace(link.LastError))
        {
            return string.Empty;
        }

        var attempts = link.ConsecutiveFailures > 0
            ? $"{link.ConsecutiveFailures} failed attempt{(link.ConsecutiveFailures == 1 ? string.Empty : "s")}"
            : string.Empty;

        var error = string.IsNullOrWhiteSpace(link.LastError) ? string.Empty : link.LastError!.Trim();

        return (attempts, error) switch
        {
            ("", "") => string.Empty,
            (_, "") => attempts + ".",
            ("", _) => error,
            _ => $"{attempts}: {error}",
        };
    }

    private static string Append(string head, string tail) =>
        string.IsNullOrEmpty(tail) ? head : $"{head} {tail}";

    internal static string BadgeFor(int total) =>
        total > 99 ? "99+" : total.ToString(CultureInfo.InvariantCulture);

    /// <summary>
    /// Clamps to the Shell_NotifyIcon tooltip limit. Truncating in our own code
    /// keeps the leading words — which carry the state — rather than letting
    /// the shell cut wherever it likes.
    /// </summary>
    internal static string Clamp(string text)
    {
        if (text.Length <= TrayState.TooltipMaxLength)
        {
            return text;
        }

        return string.Concat(text.AsSpan(0, TrayState.TooltipMaxLength - 1), "…");
    }
}
