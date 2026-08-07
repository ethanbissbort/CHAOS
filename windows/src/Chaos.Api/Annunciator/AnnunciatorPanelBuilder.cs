using System.Text.Json;
using System.Text.Json.Nodes;
using Chaos.Api.Data;

namespace Chaos.Api.Annunciator;

/// <summary>
/// Turns a database snapshot into the panel. Pure: no I/O, no clock.
/// </summary>
/// <remarks>
/// Port of <c>annunciator_panel()</c> in
/// <c>src/homestead_twin/api/routers/annunciator.py</c>. Keeping it free of I/O
/// is what lets the serviceability rules be tested exhaustively instead of only
/// through whatever the seeded design package happens to contain.
/// </remarks>
public static class AnnunciatorPanelBuilder
{
    /// <summary>Key under which the Python loader packs its structured extras into <c>notes</c>.</summary>
    private const string MetaKey = "meta";

    /// <summary>
    /// Builds the panel.
    /// </summary>
    /// <param name="snapshot">Everything read from the database for this request.</param>
    /// <param name="generatedAt">The server clock, supplied by the caller so this stays pure.</param>
    public static AnnunciatorPanel Build(AnnunciatorSnapshot snapshot, PlatformTimestamp generatedAt)
    {
        ArgumentNullException.ThrowIfNull(snapshot);

        // Most recent open alarm wins the tile; the panel is one lamp per
        // condition. The rest are counted, not shown.
        var newest = new Dictionary<string, AlarmRow>(StringComparer.Ordinal);
        var extraCounts = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var alarm in snapshot.OpenAlarms)
        {
            if (newest.ContainsKey(alarm.AlarmKey))
            {
                extraCounts[alarm.AlarmKey] = extraCounts.GetValueOrDefault(alarm.AlarmKey) + 1;
                continue;
            }

            newest[alarm.AlarmKey] = alarm;
        }

        var counts = new Dictionary<string, int>(StringComparer.Ordinal)
        {
            [TileState.Normal] = 0,
            [TileState.Alarm] = 0,
            [TileState.Acknowledged] = 0,
            [TileState.Ringback] = 0,
            [TileState.Inhibited] = 0,
            [TileState.OutOfService] = 0,
        };

        // Insertion-ordered, mirroring Python's dict, so bays not named in the
        // wall order keep a defined position before the alphabetical pass.
        var bays = new Dictionary<string, List<PanelTile>>(StringComparer.Ordinal);
        var bayOrderSeen = new List<string>();
        var total = 0;

        foreach (var definition in snapshot.Definitions)
        {
            var serviceability = Serviceability.Evaluate(
                definition,
                snapshot.KnownPointIds,
                snapshot.AssetIdsByClass);

            newest.TryGetValue(definition.AlarmKey, out var alarm);
            var state = ResolveTileState(alarm, serviceability.Serviceable);

            var tile = new PanelTile
            {
                AlarmKey = definition.AlarmKey,
                Legend = Legends.Engrave(definition.Name, definition.AlarmKey),
                Name = definition.Name,
                Severity = definition.Severity,
                Domain = definition.Domain,
                State = state,
                Serviceable = serviceability.Serviceable,
                ServiceNote = serviceability.Reason,
                RequiresManualReset = definition.RequiresManualReset,
                ThresholdStatus = ReadThresholdStatus(definition.NotesJson),
                AlarmId = alarm?.Id,
                Since = alarm is null ? null : alarm.ActivatedAt ?? alarm.DetectedAt,
                Message = alarm?.Message,
                IncidentId = alarm?.IncidentId,
                SuppressionReason = alarm?.SuppressionReason,
                DuplicateOpenCount = extraCounts.GetValueOrDefault(definition.AlarmKey),
            };

            // `domain` is nullable in the schema. Python would raise on a null
            // here (it calls .upper() on it); this side groups such tiles under
            // an empty-string bay so the panel still renders every definition.
            // No shipped definition has a null domain — see the note in
            // docs/dotnet-migration.md.
            var domainKey = definition.Domain ?? string.Empty;
            if (!bays.TryGetValue(domainKey, out var tiles))
            {
                tiles = [];
                bays[domainKey] = tiles;
                bayOrderSeen.Add(domainKey);
            }

            tiles.Add(tile);

            total++;
            counts[state] = counts.GetValueOrDefault(state) + 1;
        }

        var summary = new PanelSummary
        {
            Total = total,
            Normal = counts[TileState.Normal],
            Alarm = counts[TileState.Alarm],
            Acknowledged = counts[TileState.Acknowledged],
            Ringback = counts[TileState.Ringback],
            Inhibited = counts[TileState.Inhibited],
            OutOfService = counts[TileState.OutOfService],

            // Reported rather than left to the UI to infer, so every client
            // agrees on when the room is making noise.
            Horn = counts[TileState.Alarm] > 0,
            RingbackTone = counts[TileState.Ringback] > 0,
        };

        return new AnnunciatorPanel
        {
            GeneratedAt = generatedAt,
            Bays = OrderBays(bays, bayOrderSeen),
            Summary = summary,
            TileStates = TileState.Descriptions,
        };
    }

    /// <summary>
    /// The annunciator state for a tile, given its newest open alarm.
    /// </summary>
    /// <remarks>
    /// Serviceability is checked FIRST and unconditionally. An unserviceable
    /// tile is out of service even if an alarm row exists for it, because the
    /// tile's claim about the world is what the operator reads.
    /// </remarks>
    internal static string ResolveTileState(AlarmRow? alarm, bool serviceable)
    {
        if (!serviceable)
        {
            return TileState.OutOfService;
        }

        if (alarm is null)
        {
            return TileState.Normal;
        }

        if (alarm.Suppressed)
        {
            return TileState.Inhibited;
        }

        return alarm.State switch
        {
            // Condition gone, operator has not closed it out: classic ringback.
            "cleared" or "mitigated" => TileState.Ringback,
            "acknowledged" => TileState.Acknowledged,
            _ => TileState.Alarm,
        };
    }

    /// <summary>
    /// Reads <c>threshold_status</c> out of the definition's <c>notes</c> array.
    /// </summary>
    /// <remarks>
    /// The Python loader carries the structured extras as a single
    /// <c>{"meta": {...}}</c> entry appended to <c>notes</c>, so no contract-layer
    /// column had to change. Mirrors <c>definition_meta()</c>: first entry that
    /// is an object with a <c>meta</c> key wins, and a missing key yields null.
    /// The value is passed through as raw JSON rather than coerced to a string,
    /// so a non-string value round-trips instead of being silently reshaped.
    /// </remarks>
    internal static JsonNode? ReadThresholdStatus(string? notesJson)
    {
        if (string.IsNullOrWhiteSpace(notesJson))
        {
            return null;
        }

        JsonNode? parsed;
        try
        {
            parsed = JsonNode.Parse(notesJson);
        }
        catch (JsonException ex)
        {
            throw new ChaosDataException(
                $"alarm_definitions.notes is not valid JSON: {ex.Message}",
                ex);
        }

        if (parsed is not JsonArray notes)
        {
            return null;
        }

        foreach (var note in notes)
        {
            if (note is not JsonObject entry || !entry.TryGetPropertyValue(MetaKey, out var meta))
            {
                continue;
            }

            if (meta is not JsonObject metaObject)
            {
                return null;
            }

            return metaObject.TryGetPropertyValue("threshold_status", out var status)
                ? status?.DeepClone()
                : null;
        }

        return null;
    }

    /// <summary>
    /// Bays in wall order: declared order first, then any remaining domain
    /// alphabetically so a new domain can never silently vanish from the panel.
    /// </summary>
    private static List<PanelBay> OrderBays(
        Dictionary<string, List<PanelTile>> bays,
        List<string> insertionOrder)
    {
        var titles = PanelBays.Order.ToDictionary(entry => entry.Domain, entry => entry.Title, StringComparer.Ordinal);
        var ordered = new List<PanelBay>(bays.Count);
        var placed = new HashSet<string>(StringComparer.Ordinal);

        foreach (var (domain, title) in PanelBays.Order)
        {
            if (bays.TryGetValue(domain, out var tiles))
            {
                ordered.Add(BuildBay(domain, title, tiles));
                placed.Add(domain);
            }
        }

        // Ordinal sort, matching Python's sorted() over str keys. A
        // culture-aware sort would reorder the wall between platforms.
        var remaining = insertionOrder
            .Where(domain => !placed.Contains(domain))
            .Order(StringComparer.Ordinal);

        foreach (var domain in remaining)
        {
            var title = titles.TryGetValue(domain, out var declared) ? declared : domain.ToUpperInvariant();
            ordered.Add(BuildBay(domain, title, bays[domain]));
        }

        return ordered;
    }

    private static PanelBay BuildBay(string domain, string title, List<PanelTile> tiles)
    {
        // Severity first, then key: the eye should land on the worst thing in
        // each bay, and the position must stay stable between polls.
        var sorted = tiles
            .OrderBy(tile => PanelBays.SeverityRank(tile.Severity))
            .ThenBy(tile => tile.AlarmKey, StringComparer.Ordinal)
            .ToList();

        return new PanelBay
        {
            Domain = domain,
            Title = title,
            Tiles = sorted,
        };
    }
}
