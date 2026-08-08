using System.Text.Json;

namespace Chaos.Host.Setup;

/// <summary>
/// The platform's own account of its database, parsed from
/// <c>chaos status --json</c>.
/// </summary>
/// <remarks>
/// <para>
/// The gateway asks the platform rather than opening the database itself. A C#
/// reimplementation of "which tables should exist" would be a second source of
/// truth, and the first thing to drift when a model is added in Python.
/// </para>
/// <para>
/// Every field is nullable or explicitly empty. A count that is absent from the
/// payload means the table is not there, which is not the same as a table with
/// no rows, and the two must never collapse into <c>0</c>.
/// </para>
/// </remarks>
internal sealed record PlatformStatusReading
{
    /// <summary>Counts the payload carries, keyed by table name. Absent means "no such table".</summary>
    public IReadOnlyDictionary<string, int> Counts { get; init; } =
        new Dictionary<string, int>(StringComparer.Ordinal);

    /// <summary>Declared tables the database does not have.</summary>
    public IReadOnlyList<string> MissingTables { get; init; } = [];

    /// <summary>The database URL the platform used, with any password redacted. Null if it did not say.</summary>
    public string? DatabaseUrl { get; init; }

    /// <summary>Whether the platform could open the database, or null if it did not say.</summary>
    public bool? DatabaseReachable { get; init; }

    /// <summary>The platform version, or null if it did not say.</summary>
    public string? PlatformVersion { get; init; }

    /// <summary>The node role, or null if it did not say.</summary>
    public string? NodeRole { get; init; }

    /// <summary>A row count, or null when the table is not present in the database.</summary>
    /// <param name="table">The table name.</param>
    /// <returns>The count, or null.</returns>
    public int? Count(string table) => Counts.TryGetValue(table, out var value) ? value : null;

    /// <summary>Total rows across every table the platform reported on.</summary>
    public int TotalRows => Counts.Values.Sum();

    /// <summary>Whether every declared table exists.</summary>
    public bool SchemaComplete => MissingTables.Count == 0;

    /// <summary>Parses the <c>status --json</c> payload.</summary>
    /// <param name="json">The stdout of the command.</param>
    /// <param name="reading">The parsed reading, or null.</param>
    /// <param name="problem">Why it could not be parsed, or null.</param>
    /// <returns>Whether parsing succeeded.</returns>
    public static bool TryParse(string? json, out PlatformStatusReading? reading, out string? problem)
    {
        reading = null;
        problem = null;

        if (string.IsNullOrWhiteSpace(json))
        {
            problem = "The platform CLI printed nothing. 'status --json' is expected to write a JSON object to stdout.";
            return false;
        }

        // The CLI configures logging to stderr, but a subsystem that prints a
        // banner to stdout would otherwise make a good payload unparseable.
        var start = json.IndexOf('{', StringComparison.Ordinal);
        var end = json.LastIndexOf('}');
        if (start < 0 || end <= start)
        {
            problem = "The platform CLI did not print a JSON object. Its output was: "
                    + Truncate(json.Trim(), 400);
            return false;
        }

        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(json[start..(end + 1)]);
        }
        catch (JsonException ex)
        {
            problem = $"The output of 'status --json' was not valid JSON: {ex.Message}";
            return false;
        }

        using (document)
        {
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                problem = "The output of 'status --json' was JSON but not an object.";
                return false;
            }

            reading = new PlatformStatusReading
            {
                Counts = ReadCounts(root),
                MissingTables = ReadStrings(root, "missing_tables"),
                DatabaseUrl = ReadString(root, "database"),
                DatabaseReachable = ReadBool(root, "database_reachable"),
                PlatformVersion = ReadString(root, "platform_version"),
                NodeRole = ReadString(root, "node_role"),
            };
            return true;
        }
    }

    private static IReadOnlyDictionary<string, int> ReadCounts(JsonElement root)
    {
        var counts = new Dictionary<string, int>(StringComparer.Ordinal);
        if (!root.TryGetProperty("counts", out var element) || element.ValueKind != JsonValueKind.Object)
        {
            return counts;
        }

        foreach (var property in element.EnumerateObject())
        {
            if (property.Value.ValueKind == JsonValueKind.Number && property.Value.TryGetInt32(out var value))
            {
                counts[property.Name] = value;
            }
        }

        return counts;
    }

    private static IReadOnlyList<string> ReadStrings(JsonElement root, string name)
    {
        if (!root.TryGetProperty(name, out var element) || element.ValueKind != JsonValueKind.Array)
        {
            return [];
        }

        var values = new List<string>();
        foreach (var item in element.EnumerateArray())
        {
            if (item.ValueKind == JsonValueKind.String && item.GetString() is { Length: > 0 } text)
            {
                values.Add(text);
            }
        }

        return values;
    }

    private static string? ReadString(JsonElement root, string name) =>
        root.TryGetProperty(name, out var element) && element.ValueKind == JsonValueKind.String
            ? element.GetString()
            : null;

    private static bool? ReadBool(JsonElement root, string name) =>
        root.TryGetProperty(name, out var element) && element.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? element.GetBoolean()
            : null;

    private static string Truncate(string value, int limit) =>
        value.Length <= limit ? value : value[..limit] + "…";
}
