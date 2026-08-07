using System.Text.Json;
using System.Text.Json.Serialization;

namespace Chaos.Shell.Core;

/// <summary>Which window a launch is asking for.</summary>
public enum ActivationTarget
{
    /// <summary>Restore and focus the operator console.</summary>
    Console = 0,

    /// <summary>Open (or focus) the annunciator panel window.</summary>
    Annunciator = 1,

    /// <summary>
    /// Operator wants to acknowledge something. Opens the annunciator, because
    /// acknowledgement is an audited write made on the panel with a named
    /// operator — the shell never acknowledges anything by itself.
    /// </summary>
    Acknowledge = 2,
}

/// <summary>
/// What a second launch sends to the instance already running.
/// </summary>
/// <remarks>
/// Wire format is one line of JSON over a named pipe. It carries a version so
/// that a newly installed build launched while an older process is still
/// resident degrades to "focus the console" instead of being misread.
/// </remarks>
public sealed record ActivationPayload
{
    /// <summary>Bump only for an incompatible change to the fields below.</summary>
    public const int CurrentVersion = 1;

    /// <summary>The safe interpretation of anything we cannot understand.</summary>
    public static readonly ActivationPayload FocusConsole = new();

    [JsonPropertyName("v")]
    public int Version { get; init; } = CurrentVersion;

    [JsonPropertyName("target")]
    [JsonConverter(typeof(JsonStringEnumConverter<ActivationTarget>))]
    public ActivationTarget Target { get; init; } = ActivationTarget.Console;

    /// <summary>Monitor request for the annunciator, e.g. "2" or "primary".</summary>
    [JsonPropertyName("monitor")]
    public string? Monitor { get; init; }

    /// <summary>Open the annunciator full-screen for a wall display.</summary>
    [JsonPropertyName("fullScreen")]
    public bool FullScreen { get; init; }

    /// <summary>Keep the annunciator above other windows.</summary>
    [JsonPropertyName("alwaysOnTop")]
    public bool AlwaysOnTop { get; init; }

    private static readonly JsonSerializerOptions SerializerOptions = new()
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = false,
    };

    /// <summary>Serialises to a single line, safe to write to a pipe.</summary>
    public string Serialize() => JsonSerializer.Serialize(this, SerializerOptions);

    /// <summary>
    /// Parses a payload. Returns false with <paramref name="payload"/> set to
    /// <see cref="FocusConsole"/> when the text is missing, malformed or from a
    /// version this build does not know.
    /// </summary>
    public static bool TryParse(string? text, out ActivationPayload payload, out string? problem)
    {
        payload = FocusConsole;
        problem = null;

        if (string.IsNullOrWhiteSpace(text))
        {
            problem = "the activation message was empty";
            return false;
        }

        ActivationPayload? parsed;
        try
        {
            parsed = JsonSerializer.Deserialize<ActivationPayload>(text!, SerializerOptions);
        }
        catch (JsonException ex)
        {
            problem = $"the activation message was not valid JSON: {ex.Message}";
            return false;
        }

        if (parsed is null)
        {
            problem = "the activation message deserialised to nothing";
            return false;
        }

        if (parsed.Version != CurrentVersion)
        {
            problem = $"activation protocol v{parsed.Version} is not understood by this build (v{CurrentVersion})";
            return false;
        }

        payload = parsed;
        return true;
    }

    /// <summary>
    /// Parses, falling back to focusing the console. A second launch must never
    /// be able to crash the running instance, whatever it sends.
    /// </summary>
    public static ActivationPayload ParseOrFocusConsole(string? text) =>
        TryParse(text, out var payload, out _) ? payload : FocusConsole;

    /// <summary>Whether this activation wants the annunciator window.</summary>
    public bool WantsAnnunciator =>
        Target is ActivationTarget.Annunciator or ActivationTarget.Acknowledge;

    /// <summary>
    /// Renders back to command-line arguments.
    /// </summary>
    /// <remarks>
    /// This is the on-wire form for single-instance activation. WinUI's
    /// <c>AppInstance.RedirectActivationToAsync</c> carries a launch command
    /// line and nothing else, so a second launch is delivered to the running
    /// instance as argv and re-parsed there by <see cref="ShellCommandLine"/>.
    /// Round-tripping through this method is what makes "chaos-shell
    /// --annunciator --monitor 2" behave the same whether or not the shell was
    /// already running.
    /// </remarks>
    public IReadOnlyList<string> ToArguments()
    {
        var args = new List<string>(6);

        switch (Target)
        {
            case ActivationTarget.Annunciator:
                args.Add("--annunciator");
                break;
            case ActivationTarget.Acknowledge:
                args.Add("--acknowledge");
                break;
            case ActivationTarget.Console:
            default:
                break;
        }

        if (!string.IsNullOrWhiteSpace(Monitor))
        {
            args.Add("--monitor");
            args.Add(Monitor!);
        }

        if (FullScreen)
        {
            args.Add("--fullscreen");
        }

        if (AlwaysOnTop)
        {
            args.Add("--always-on-top");
        }

        return args;
    }
}
