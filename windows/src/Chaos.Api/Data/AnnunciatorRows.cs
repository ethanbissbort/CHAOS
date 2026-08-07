namespace Chaos.Api.Data;

/// <summary>
/// The columns of <c>alarm_definitions</c> the annunciator panel reads.
/// </summary>
/// <remarks>
/// A narrow projection on purpose. The Python table has 29 columns; the panel
/// needs 11 of them. Mapping the other 18 would create a second description of
/// a schema this side does not own, and every one of them would be a thing that
/// can silently drift.
/// </remarks>
/// <param name="AlarmKey">Primary key — the definition's stable identity.</param>
/// <param name="Name">Human-readable name, engraved into the tile legend.</param>
/// <param name="Severity">SDD 14.1 severity: info, warning, major, critical, emergency.</param>
/// <param name="Domain">Panel bay this tile hangs in.</param>
/// <param name="PointName">The trigger point's canonical name, if the alarm has one.</param>
/// <param name="AssetId">Asset-scoped alarms name exactly one asset.</param>
/// <param name="AssetClass">Class-scoped alarms watch every asset of a class.</param>
/// <param name="TriggerExpression">Expression-driven alarms have no single trigger point.</param>
/// <param name="RequiresManualReset">Whether the operator must reset it by hand.</param>
/// <param name="Enabled">A disabled definition can never light and is out of service.</param>
/// <param name="NotesJson">
/// The raw <c>notes</c> JSON array. Carries the <c>{"meta": {...}}</c> block the
/// Python loader packs the threshold provenance into.
/// </param>
public sealed record AlarmDefinitionRow(
    string AlarmKey,
    string Name,
    string Severity,
    string? Domain,
    string? PointName,
    string? AssetId,
    string? AssetClass,
    string? TriggerExpression,
    bool RequiresManualReset,
    bool Enabled,
    string? NotesJson);

/// <summary>
/// The columns of <c>alarms</c> the panel reads for an open occurrence.
/// </summary>
/// <param name="Id">Alarm UUID.</param>
/// <param name="AlarmKey">Definition this occurrence belongs to.</param>
/// <param name="State">Lifecycle state (SDD 14): detected, active, acknowledged, mitigated, cleared, reviewed.</param>
/// <param name="Suppressed">Suppressed alarms are recorded and deliberately not annunciated.</param>
/// <param name="SuppressionReason">Why it was suppressed.</param>
/// <param name="Message">Operator-facing message.</param>
/// <param name="IncidentId">Correlation parent, when the alarm was grouped into an incident.</param>
/// <param name="DetectedAt">When the condition was first seen. Orders "newest wins" for the tile.</param>
/// <param name="ActivatedAt">When it passed its on-delay, if it did.</param>
public sealed record AlarmRow(
    string Id,
    string AlarmKey,
    string State,
    bool Suppressed,
    string? SuppressionReason,
    string? Message,
    string? IncidentId,
    PlatformTimestamp DetectedAt,
    PlatformTimestamp? ActivatedAt);

/// <summary>
/// Everything one annunciator request reads from the database, in one shot.
/// </summary>
/// <remarks>
/// Materialising the snapshot before computing anything keeps the panel logic
/// pure and unit-testable without a database, which is what lets the
/// serviceability rules — the part with a safety consequence — be tested
/// exhaustively rather than only through whatever the seeded fixture happens to
/// contain.
/// </remarks>
/// <param name="Definitions">Every definition, ordered by <c>alarm_key</c>.</param>
/// <param name="KnownPointIds">Every <c>&lt;asset_id&gt;/&lt;point_name&gt;</c> that exists.</param>
/// <param name="AssetIdsByClass">Asset IDs grouped by asset class, for class-scoped alarms.</param>
/// <param name="OpenAlarms">Open alarms in scope, newest <c>detected_at</c> first.</param>
public sealed record AnnunciatorSnapshot(
    IReadOnlyList<AlarmDefinitionRow> Definitions,
    IReadOnlySet<string> KnownPointIds,
    IReadOnlyDictionary<string, IReadOnlyList<string>> AssetIdsByClass,
    IReadOnlyList<AlarmRow> OpenAlarms);
