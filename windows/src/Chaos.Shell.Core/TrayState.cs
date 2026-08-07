namespace Chaos.Shell.Core;

/// <summary>
/// What the tray icon should look like. The three "we cannot vouch for this"
/// states (<see cref="Starting"/>, <see cref="Unreachable"/>,
/// <see cref="Stale"/>) are separate from <see cref="Normal"/> on purpose and
/// must never be rendered with the same artwork.
/// </summary>
public enum TrayIconState
{
    /// <summary>Shell is up, gateway not yet reached. Alarm state unknown.</summary>
    Starting = 0,

    /// <summary>Contact lost. Alarm state unknown — NOT "no alarms".</summary>
    Unreachable = 1,

    /// <summary>Last answer is older than the freshness budget. Shown as last-known.</summary>
    Stale = 2,

    /// <summary>Current answer: nothing active.</summary>
    Normal = 3,

    /// <summary>Current answer: worst active severity is warning.</summary>
    Warning = 4,

    /// <summary>Current answer: worst active severity is major or critical.</summary>
    Alarm = 5,

    /// <summary>Current answer: at least one emergency alarm is active.</summary>
    Emergency = 6,
}

/// <summary>What the small numeric overlay on the tray icon shows.</summary>
public enum TrayBadgeKind
{
    /// <summary>Nothing to overlay.</summary>
    None = 0,

    /// <summary>A count of active alarms.</summary>
    Count = 1,

    /// <summary>
    /// A question mark: the shell cannot see the platform, so it has no count
    /// to give. Never substituted with "0".
    /// </summary>
    Unknown = 2,
}

/// <summary>
/// The complete, ready-to-render tray presentation. Produced only by
/// <see cref="TrayStateFactory"/> so that the disconnected-versus-quiet
/// distinction is decided in exactly one place.
/// </summary>
public sealed record TrayState
{
    public required TrayIconState Icon { get; init; }

    /// <summary>
    /// Tooltip text, already clamped to <see cref="TooltipMaxLength"/>.
    /// </summary>
    public required string Tooltip { get; init; }

    /// <summary>Single-line heading for the top of the tray context menu.</summary>
    public required string Headline { get; init; }

    /// <summary>Second menu line: freshness, or why there is nothing to show.</summary>
    public required string SubHeadline { get; init; }

    public TrayBadgeKind BadgeKind { get; init; } = TrayBadgeKind.None;

    /// <summary>Badge glyph: "7", "99+", "?" or "".</summary>
    public string BadgeText { get; init; } = string.Empty;

    /// <summary>
    /// True when the badge is a last-known value rather than a current one.
    /// The renderer draws these dimmed so a stale count cannot be mistaken for
    /// a live one.
    /// </summary>
    public bool BadgeIsProvisional { get; init; }

    /// <summary>Whether the icon should pulse for attention.</summary>
    public bool Blink { get; init; }

    /// <summary>
    /// True only when the shell has a current answer from the platform. Any UI
    /// that would otherwise imply "all good" must check this first.
    /// </summary>
    public required bool DataIsCurrent { get; init; }

    /// <summary>
    /// Windows Shell_NotifyIcon truncates tooltips past this length.
    /// </summary>
    public const int TooltipMaxLength = 127;
}
