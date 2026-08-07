using System.Text.Json;

namespace Chaos.Shell.Core;

/// <summary>
/// Reads <c>GET /api/v1/alarms/active</c> into <see cref="AlarmCounts"/>.
/// </summary>
/// <remarks>
/// Parsing lives here, not in the WPF project, so the shapes the platform
/// actually returns can be pinned by tests. The reader is deliberately strict
/// about failure: a payload it cannot understand produces
/// <see langword="false"/> and a reason, and the caller keeps the link in a
/// failed state. Guessing at a half-read payload would put invented numbers on
/// an alarm panel.
/// </remarks>
public static class AlarmSummaryReader
{
    /// <summary>Alarm states that count as active but not yet acknowledged.</summary>
    private const string UnacknowledgedState = "active";

    public static bool TryRead(string json, out AlarmCounts counts, out string? problem)
    {
        counts = AlarmCounts.None;
        problem = null;

        if (string.IsNullOrWhiteSpace(json))
        {
            problem = "the alarm response was empty";
            return false;
        }

        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(json);
        }
        catch (JsonException ex)
        {
            problem = $"the alarm response was not valid JSON: {ex.Message}";
            return false;
        }

        using (document)
        {
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                problem = "the alarm response was not a JSON object";
                return false;
            }

            var bySeverity = ReadSeverityMap(root);
            if (bySeverity is null)
            {
                problem = "the alarm response had no usable 'by_severity' map";
                return false;
            }

            var parsed = AlarmCounts.FromSeverityMap(bySeverity);

            // If the server's own total exceeds what the severity map accounts
            // for, the surplus is carried as unclassified rather than dropped.
            // An alarm this shell cannot categorise still has to be visible.
            var reportedTotal = ReadInt(root, "count");
            var surplus = reportedTotal is { } total ? total - parsed.Total : 0;

            counts = parsed with
            {
                Unclassified = parsed.Unclassified + Math.Max(surplus, 0),
                Suppressed = ReadInt(root, "suppressed_count") ?? 0,
                Unacknowledged = ReadUnacknowledged(root),
            };

            return true;
        }
    }

    private static Dictionary<string, int>? ReadSeverityMap(JsonElement root)
    {
        if (!root.TryGetProperty("by_severity", out var map))
        {
            // An empty site legitimately returns an empty map, but a response
            // missing the property entirely is a different API than we expect.
            return null;
        }

        if (map.ValueKind != JsonValueKind.Object)
        {
            return null;
        }

        var result = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        foreach (var property in map.EnumerateObject())
        {
            if (property.Value.ValueKind == JsonValueKind.Number
                && property.Value.TryGetInt32(out var value))
            {
                result[property.Name] = value;
            }
        }

        return result;
    }

    private static int? ReadUnacknowledged(JsonElement root)
    {
        if (!root.TryGetProperty("alarms", out var alarms) || alarms.ValueKind != JsonValueKind.Array)
        {
            // Without the alarm list there is no way to tell acknowledged from
            // unacknowledged. Null means unknown and is rendered as such.
            return null;
        }

        var unacknowledged = 0;
        foreach (var alarm in alarms.EnumerateArray())
        {
            if (alarm.ValueKind != JsonValueKind.Object)
            {
                continue;
            }

            if (alarm.TryGetProperty("state", out var state)
                && state.ValueKind == JsonValueKind.String
                && string.Equals(state.GetString(), UnacknowledgedState, StringComparison.OrdinalIgnoreCase))
            {
                unacknowledged++;
            }
        }

        return unacknowledged;
    }

    private static int? ReadInt(JsonElement root, string property) =>
        root.TryGetProperty(property, out var element)
        && element.ValueKind == JsonValueKind.Number
        && element.TryGetInt32(out var value)
            ? value
            : null;
}
