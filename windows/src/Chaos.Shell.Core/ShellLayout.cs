using System.Text.Json.Serialization;

namespace Chaos.Shell.Core;

/// <summary>The window layout remembered across restarts.</summary>
public sealed record ShellLayout
{
    public static readonly ShellLayout Empty = new();

    /// <summary>Schema version of the persisted file.</summary>
    [JsonPropertyName("v")]
    public int Version { get; init; } = 1;

    [JsonPropertyName("main")]
    public WindowPlacement? Main { get; init; }

    [JsonPropertyName("annunciator")]
    public WindowPlacement? Annunciator { get; init; }

    /// <summary>
    /// Whether the annunciator was open when the shell last exited, so a wall
    /// display comes back up after a reboot without anyone walking over to it.
    /// </summary>
    [JsonPropertyName("annunciatorOpen")]
    public bool AnnunciatorWasOpen { get; init; }

    /// <summary>Last gateway address the operator typed, for the address box.</summary>
    [JsonPropertyName("lastHost")]
    public string? LastHost { get; init; }
}
