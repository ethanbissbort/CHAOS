using System.Text.Json;

namespace Chaos.Shell.Core;

/// <summary>
/// What <c>GET /health</c> reports about the node behind the gateway.
/// </summary>
public sealed record PlatformHealth
{
    public required string Status { get; init; }

    public string? Version { get; init; }

    public string? NodeRole { get; init; }

    public string? SiteId { get; init; }

    /// <summary>
    /// Null when the platform did not say. The shell shows "unknown" for null
    /// and never assumes the safe-looking answer in either direction.
    /// </summary>
    public bool? PhysicalControlEnabled { get; init; }

    /// <summary>
    /// The gateway's <c>backend</c> field — <c>up</c>, <c>down</c> or
    /// <c>starting</c> — exactly as it was sent. Null when this response came
    /// from something that does not report one.
    /// </summary>
    public string? Backend { get; init; }

    /// <summary>The gateway's sentence about the backend, when it sent one.</summary>
    public string? BackendDetail { get; init; }

    /// <summary>
    /// <see cref="Backend"/> mapped to a state. A word this shell does not
    /// recognise becomes <see cref="BackendState.Unknown"/>, never
    /// <see cref="BackendState.Up"/>: an unreadable answer about the process
    /// that runs the alarm engine is not an assurance that it is running.
    /// </summary>
    public BackendState BackendState => Backend?.Trim().ToLowerInvariant() switch
    {
        "up" or "ok" or "running" or "healthy" => BackendState.Up,
        "starting" or "start_pending" or "startpending" => BackendState.Starting,
        "down" or "stopped" or "failed" or "unavailable" => BackendState.Down,
        _ => BackendState.Unknown,
    };

    public bool IsOk => string.Equals(Status, "ok", StringComparison.OrdinalIgnoreCase);

    /// <summary>
    /// Reads a <c>/health</c> body. A response that parses but does not say
    /// <c>ok</c> is returned as-is, so the caller can show the platform's own
    /// word for its condition rather than a substitute.
    /// </summary>
    public static bool TryRead(string json, out PlatformHealth? health, out string? problem)
    {
        health = null;
        problem = null;

        if (string.IsNullOrWhiteSpace(json))
        {
            problem = "the health response was empty";
            return false;
        }

        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(json);
        }
        catch (JsonException ex)
        {
            problem = $"the health response was not valid JSON: {ex.Message}";
            return false;
        }

        using (document)
        {
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                problem = "the health response was not a JSON object";
                return false;
            }

            var status = ReadString(root, "status");
            if (status is null)
            {
                problem = "the health response carried no 'status'";
                return false;
            }

            // The gateway nests its own version under "host" and the backend's
            // sentence under "backendDetail"; the Python platform answers a
            // flatter shape. Both are read, because the shell probes whichever
            // is in front of it.
            var host = root.TryGetProperty("host", out var hostElement)
                && hostElement.ValueKind == JsonValueKind.Object
                    ? hostElement
                    : default;

            var backendDetail = root.TryGetProperty("backendDetail", out var detailElement)
                && detailElement.ValueKind == JsonValueKind.Object
                    ? detailElement
                    : default;

            health = new PlatformHealth
            {
                Status = status,
                Version = ReadString(root, "version")
                    ?? (host.ValueKind == JsonValueKind.Object ? ReadString(host, "version") : null),
                NodeRole = ReadString(root, "node_role"),
                SiteId = ReadString(root, "site_id"),
                PhysicalControlEnabled = ReadBool(root, "physical_control_enabled"),
                Backend = ReadString(root, "backend"),
                BackendDetail = backendDetail.ValueKind == JsonValueKind.Object
                    ? ReadString(backendDetail, "detail")
                    : null,
            };
            return true;
        }
    }

    /// <summary>One line for the tray "Service status" item.</summary>
    public string Describe()
    {
        var role = string.IsNullOrWhiteSpace(NodeRole) ? "unknown role" : NodeRole!;
        var version = string.IsNullOrWhiteSpace(Version) ? "unknown version" : Version!;
        var control = PhysicalControlEnabled switch
        {
            true => "physical control ENABLED",
            false => "physical control disabled",
            null => "physical control unknown",
        };
        return $"{Status} · {role} · {version} · {control}";
    }

    private static string? ReadString(JsonElement root, string property) =>
        root.TryGetProperty(property, out var element) && element.ValueKind == JsonValueKind.String
            ? element.GetString()
            : null;

    private static bool? ReadBool(JsonElement root, string property) =>
        root.TryGetProperty(property, out var element)
            ? element.ValueKind switch
            {
                JsonValueKind.True => true,
                JsonValueKind.False => false,
                _ => null,
            }
            : null;
}
