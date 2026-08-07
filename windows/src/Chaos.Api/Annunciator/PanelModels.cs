using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using Chaos.Api.Data;

namespace Chaos.Api.Annunciator;

/// <summary>
/// Tile states, in the annunciator sense rather than the alarm-lifecycle sense.
/// </summary>
/// <remarks>
/// The panel is a lamp, not a database: it shows what the operator must do next.
/// The vocabulary is returned with every response so every client — browser
/// console, wall display, WinUI shell — agrees on what a tile means and on when
/// the room is making noise.
/// </remarks>
public static class TileState
{
    /// <summary>Condition normal. The tile is dark and that is a trustworthy statement.</summary>
    public const string Normal = "normal";

    /// <summary>Abnormal and unacknowledged. Fast flash with horn.</summary>
    public const string Alarm = "alarm";

    /// <summary>Abnormal, acknowledged. Steady lit, horn silenced.</summary>
    public const string Acknowledged = "acknowledged";

    /// <summary>Returned to normal but not yet reset. Slow flash with ringback tone.</summary>
    public const string Ringback = "ringback";

    /// <summary>Suppressed by maintenance mode or as a correlated symptom.</summary>
    public const string Inhibited = "inhibited";

    /// <summary>This tile cannot light. A dark tile here would be a false assurance.</summary>
    public const string OutOfService = "out_of_service";

    /// <summary>The vocabulary returned to clients, verbatim from the Python endpoint.</summary>
    public static readonly IReadOnlyDictionary<string, string> Descriptions =
        new Dictionary<string, string>(StringComparer.Ordinal)
        {
            [Normal] = "Condition normal. The tile is dark and that is a trustworthy statement.",
            [Alarm] = "Abnormal and unacknowledged. Fast flash with horn.",
            [Acknowledged] = "Abnormal, acknowledged. Steady lit, horn silenced.",
            [Ringback] = "Returned to normal but not yet reset. Slow flash with ringback tone.",
            [Inhibited] = "Suppressed by maintenance mode or as the symptom of a correlated incident. "
                + "Recorded, deliberately not annunciated.",
            [OutOfService] = "This tile cannot light. Disabled, or its trigger point does not exist for its "
                + "asset. A dark tile here would be a false assurance.",
        };
}

/// <summary>
/// Panel bays, in the order they are hung on the wall.
/// </summary>
/// <remarks>
/// Domains not listed here still get a bay, appended in alphabetical order, so
/// a new domain can never silently vanish from the panel.
/// </remarks>
public static class PanelBays
{
    /// <summary>The declared wall order: domain key and engraved bay title.</summary>
    public static readonly IReadOnlyList<(string Domain, string Title)> Order =
    [
        ("energy", "ENERGY AND POWER"),
        ("safety", "SAFETY"),
        ("structure", "CONTAINER AND STRUCTURE"),
        ("it", "SERVER, NETWORK AND COMMS"),
        ("security", "SECURITY"),
        ("water", "WATER"),
        ("agriculture", "AGRICULTURE"),
        ("storage", "STORAGE"),
        ("spa", "SPA"),
        ("platform", "PLATFORM"),
    ];

    /// <summary>
    /// Severity sort order inside a bay: the eye should land on the worst thing
    /// first, and an unknown severity sorts last rather than crashing.
    /// </summary>
    public static int SeverityRank(string? severity) => severity switch
    {
        "emergency" => 0,
        "critical" => 1,
        "major" => 2,
        "warning" => 3,
        "info" => 4,
        _ => 99,
    };
}

/// <summary>One engraved window on the panel.</summary>
public sealed record PanelTile
{
    /// <summary>The alarm definition's key.</summary>
    [JsonPropertyName("alarm_key")]
    public required string AlarmKey { get; init; }

    /// <summary>The engraved legend, one entry per line.</summary>
    [JsonPropertyName("legend")]
    public required IReadOnlyList<string> Legend { get; init; }

    /// <summary>The definition's full name.</summary>
    [JsonPropertyName("name")]
    public required string Name { get; init; }

    /// <summary>SDD 14.1 severity.</summary>
    [JsonPropertyName("severity")]
    public required string Severity { get; init; }

    /// <summary>The bay this tile hangs in.</summary>
    [JsonPropertyName("domain")]
    public required string? Domain { get; init; }

    /// <summary>One of <see cref="TileState"/>.</summary>
    [JsonPropertyName("state")]
    public required string State { get; init; }

    /// <summary>Whether this tile is capable of lighting.</summary>
    [JsonPropertyName("serviceable")]
    public required bool Serviceable { get; init; }

    /// <summary>Why it is not serviceable; null when it is.</summary>
    [JsonPropertyName("service_note")]
    public required string? ServiceNote { get; init; }

    /// <summary>Whether the operator must reset it by hand.</summary>
    [JsonPropertyName("requires_manual_reset")]
    public required bool RequiresManualReset { get; init; }

    /// <summary>
    /// Threshold provenance from the definition's metadata. A numeric trip point
    /// that does not say where it came from reads as a decided value; it is not
    /// one, and the panel carries the label everywhere the number surfaces.
    /// </summary>
    [JsonPropertyName("threshold_status")]
    public required JsonNode? ThresholdStatus { get; init; }

    /// <summary>The open alarm lighting this tile, if any.</summary>
    [JsonPropertyName("alarm_id")]
    public required string? AlarmId { get; init; }

    /// <summary>When the alarm activated, falling back to when it was detected.</summary>
    [JsonPropertyName("since")]
    public required PlatformTimestamp? Since { get; init; }

    /// <summary>The alarm's operator-facing message.</summary>
    [JsonPropertyName("message")]
    public required string? Message { get; init; }

    /// <summary>The incident this alarm was correlated into.</summary>
    [JsonPropertyName("incident_id")]
    public required string? IncidentId { get; init; }

    /// <summary>Why the alarm is suppressed.</summary>
    [JsonPropertyName("suppression_reason")]
    public required string? SuppressionReason { get; init; }

    /// <summary>
    /// How many further open alarms share this key. The panel is one lamp per
    /// condition, so the extras are counted rather than shown.
    /// </summary>
    [JsonPropertyName("duplicate_open_count")]
    public required int DuplicateOpenCount { get; init; }
}

/// <summary>A bay: one group of windows on the wall.</summary>
public sealed record PanelBay
{
    /// <summary>Domain key.</summary>
    [JsonPropertyName("domain")]
    public required string Domain { get; init; }

    /// <summary>Engraved bay title.</summary>
    [JsonPropertyName("title")]
    public required string Title { get; init; }

    /// <summary>Tiles, worst severity first then by key, in a position that never moves.</summary>
    [JsonPropertyName("tiles")]
    public required IReadOnlyList<PanelTile> Tiles { get; init; }
}

/// <summary>Panel-wide counts and the audible state of the room.</summary>
public sealed record PanelSummary
{
    /// <summary>Total tiles, i.e. total definitions.</summary>
    [JsonPropertyName("total")]
    public required int Total { get; init; }

    /// <summary>Tiles dark and trustworthy.</summary>
    [JsonPropertyName("normal")]
    public required int Normal { get; init; }

    /// <summary>Tiles in unacknowledged alarm.</summary>
    [JsonPropertyName("alarm")]
    public required int Alarm { get; init; }

    /// <summary>Tiles acknowledged but still abnormal.</summary>
    [JsonPropertyName("acknowledged")]
    public required int Acknowledged { get; init; }

    /// <summary>Tiles awaiting reset.</summary>
    [JsonPropertyName("ringback")]
    public required int Ringback { get; init; }

    /// <summary>Tiles deliberately not annunciated.</summary>
    [JsonPropertyName("inhibited")]
    public required int Inhibited { get; init; }

    /// <summary>
    /// Tiles that cannot light. Counted separately from <see cref="Normal"/>
    /// precisely so "40 defined, 0 active" cannot be read as full coverage.
    /// </summary>
    [JsonPropertyName("out_of_service")]
    public required int OutOfService { get; init; }

    /// <summary>The horn sounds for any unacknowledged alarm.</summary>
    [JsonPropertyName("horn")]
    public required bool Horn { get; init; }

    /// <summary>The ringback tone sounds for any tile waiting to be reset.</summary>
    [JsonPropertyName("ringback_tone")]
    public required bool RingbackTone { get; init; }
}

/// <summary>The whole panel, as returned by <c>GET /api/v1/annunciator</c>.</summary>
public sealed record AnnunciatorPanel
{
    /// <summary>Server clock at the moment the panel was rendered.</summary>
    [JsonPropertyName("generated_at")]
    public required PlatformTimestamp GeneratedAt { get; init; }

    /// <summary>Bays in wall order.</summary>
    [JsonPropertyName("bays")]
    public required IReadOnlyList<PanelBay> Bays { get; init; }

    /// <summary>Counts and audible state.</summary>
    [JsonPropertyName("summary")]
    public required PanelSummary Summary { get; init; }

    /// <summary>The tile-state vocabulary, so clients need no private copy.</summary>
    [JsonPropertyName("tile_states")]
    public required IReadOnlyDictionary<string, string> TileStates { get; init; }
}
