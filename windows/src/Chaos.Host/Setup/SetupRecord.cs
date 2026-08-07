using System.Text.Json;
using System.Text.Json.Serialization;
using Chaos.Host.Configuration;

namespace Chaos.Host.Setup;

/// <summary>
/// What this gateway remembers about the last setup it performed itself.
/// </summary>
/// <remarks>
/// <para>
/// Written only after a run this host completed successfully. It is additive:
/// nothing in the platform reads it, and deleting it costs nothing but the
/// answer to "is the registry still in step with <c>data/</c>".
/// </para>
/// <para>
/// The absence of a record is reported as <em>unknown</em>, never as
/// "out of date" and never as "up to date". A machine set up from the CLI
/// before the gateway existed has no record, and the gateway says exactly that.
/// </para>
/// </remarks>
internal sealed record SetupRecord
{
    /// <summary>Schema version of this file.</summary>
    [JsonPropertyName("version")]
    public int Version { get; init; } = 1;

    /// <summary>When the gateway last completed setup.</summary>
    [JsonPropertyName("completedUtc")]
    public DateTimeOffset? CompletedUtc { get; init; }

    /// <summary>The design-package fingerprint at that moment.</summary>
    [JsonPropertyName("designPackageFingerprint")]
    public string? DesignPackageFingerprint { get; init; }

    /// <summary>The database that was set up, redacted.</summary>
    [JsonPropertyName("databaseUrl")]
    public string? DatabaseUrl { get; init; }

    /// <summary>The gateway version that did it.</summary>
    [JsonPropertyName("hostVersion")]
    public string? HostVersion { get; init; }

    private static readonly JsonSerializerOptions SerializerOptions = new()
    {
        WriteIndented = true,
    };

    /// <summary>Reads the record, if there is one.</summary>
    /// <param name="path">Where it lives, or null.</param>
    /// <returns>The record, or null when there is none or it cannot be read.</returns>
    public static SetupRecord? TryRead(string? path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
        {
            return null;
        }

        try
        {
            return JsonSerializer.Deserialize<SetupRecord>(File.ReadAllText(path), SerializerOptions);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException
                                   or JsonException or NotSupportedException or ArgumentException)
        {
            // An unreadable record is the same as no record: unknown. It is
            // never a reason to refuse to set the platform up, and never a
            // reason to claim the registry is current.
            return null;
        }
    }

    /// <summary>Writes the record. Best effort.</summary>
    /// <param name="path">Where to write it, or null to skip.</param>
    /// <param name="record">The record.</param>
    /// <returns>Null on success, or why it could not be written.</returns>
    public static string? TryWrite(string? path, SetupRecord record)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return "No setup state file is configured, so this run was not recorded. The registry is loaded; "
                 + "only the 'is it still in step with data/' answer is lost.";
        }

        try
        {
            var directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }

            File.WriteAllText(path, JsonSerializer.Serialize(record, SerializerOptions));
            return null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException
                                   or NotSupportedException or ArgumentException)
        {
            return $"Could not write the setup state file '{path}': {ex.GetType().Name}: {ex.Message}";
        }
    }

    /// <summary>Where the record lives, from configuration or derived.</summary>
    /// <param name="options">Gateway options.</param>
    /// <returns>An absolute path, or null when there is nowhere to put it.</returns>
    public static string? ResolvePath(ChaosHostOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);

        if (!string.IsNullOrWhiteSpace(options.SetupStateFile))
        {
            return Path.GetFullPath(options.SetupStateFile);
        }

        var directory = SetupLogFactory.DerivedStateDirectory();
        return directory is null ? null : Path.Combine(directory, "chaos-setup-state.json");
    }
}
