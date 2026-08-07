namespace Chaos.Shell.Core;

/// <summary>
/// The SDD 14.1 severities, ordered so that a larger value is worse. The
/// platform emits exactly these four (see <c>data/alarm_definitions.yaml</c>).
/// </summary>
public enum AlarmSeverity
{
    Warning = 1,
    Major = 2,
    Critical = 3,
    Emergency = 4,
}

/// <summary>
/// Active-alarm counts as last reported by the platform.
/// </summary>
/// <remarks>
/// This type is deliberately incapable of expressing "I don't know". A count of
/// zero means the platform said zero. Whether that answer is current at all is
/// carried separately by <see cref="LinkStatus"/>, and only
/// <see cref="TrayStateFactory"/> combines the two. Keeping the two apart is
/// what stops an unreachable backend from being rendered as a quiet site.
/// </remarks>
public sealed record AlarmCounts
{
    /// <summary>No active alarms — an answer, not an absence of one.</summary>
    public static readonly AlarmCounts None = new();

    public int Emergency { get; init; }

    public int Critical { get; init; }

    public int Major { get; init; }

    public int Warning { get; init; }

    /// <summary>
    /// Alarms whose severity string this shell does not recognise. They are
    /// never discarded: an unknown severity is treated as potentially serious
    /// by <see cref="TrayStateFactory"/> rather than silently dropped.
    /// </summary>
    public int Unclassified { get; init; }

    /// <summary>
    /// Active alarms not yet acknowledged, or <see langword="null"/> when the
    /// payload did not carry enough detail to tell. Null renders as "unknown",
    /// never as zero.
    /// </summary>
    public int? Unacknowledged { get; init; }

    /// <summary>Alarms whose notification is suppressed. Still counted above.</summary>
    public int Suppressed { get; init; }

    public int Total => Emergency + Critical + Major + Warning + Unclassified;

    public bool Any => Total > 0;

    public bool HasUnclassified => Unclassified > 0;

    /// <summary>The worst recognised severity present, or null if none are.</summary>
    public AlarmSeverity? Worst =>
        Emergency > 0 ? AlarmSeverity.Emergency
        : Critical > 0 ? AlarmSeverity.Critical
        : Major > 0 ? AlarmSeverity.Major
        : Warning > 0 ? AlarmSeverity.Warning
        : null;

    /// <summary>
    /// Builds counts from the <c>by_severity</c> map of
    /// <c>GET /api/v1/alarms/active</c>. Severity keys are matched
    /// case-insensitively; anything unrecognised lands in
    /// <see cref="Unclassified"/>.
    /// </summary>
    public static AlarmCounts FromSeverityMap(IReadOnlyDictionary<string, int> bySeverity)
    {
        ArgumentNullException.ThrowIfNull(bySeverity);

        int emergency = 0, critical = 0, major = 0, warning = 0, unclassified = 0;

        foreach (var (key, value) in bySeverity)
        {
            if (value <= 0)
            {
                continue;
            }

            switch (key?.Trim().ToLowerInvariant())
            {
                case "emergency":
                    emergency += value;
                    break;
                case "critical":
                    critical += value;
                    break;
                case "major":
                    major += value;
                    break;
                case "warning":
                    warning += value;
                    break;
                default:
                    unclassified += value;
                    break;
            }
        }

        return new AlarmCounts
        {
            Emergency = emergency,
            Critical = critical,
            Major = major,
            Warning = warning,
            Unclassified = unclassified,
        };
    }

    /// <summary>
    /// A compact "2 critical, 1 warning" phrase. Returns an empty string when
    /// there is nothing active — callers decide how to word "nothing active",
    /// because the right wording depends on whether the answer is current.
    /// </summary>
    public string Describe()
    {
        var parts = new List<string>(5);
        Add(parts, Emergency, "emergency");
        Add(parts, Critical, "critical");
        Add(parts, Major, "major");
        Add(parts, Warning, "warning");
        Add(parts, Unclassified, "unclassified");
        return string.Join(", ", parts);

        static void Add(List<string> into, int count, string label)
        {
            if (count > 0)
            {
                into.Add($"{count} {label}");
            }
        }
    }
}
