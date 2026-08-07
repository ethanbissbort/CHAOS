using System.Text.Json;

namespace Chaos.Shell.Core;

/// <summary>
/// The overall first-run setup state reported by <c>GET /host/setup</c>.
/// </summary>
public enum SetupState
{
    /// <summary>
    /// The host said something this shell does not recognise. Never rounded to
    /// <see cref="Ready"/>: a state word we cannot read is not a promise that
    /// the platform is set up.
    /// </summary>
    Unknown = 0,

    /// <summary>Setup has never run on this node.</summary>
    NotStarted = 1,

    /// <summary>The host is working out what is missing.</summary>
    Checking = 2,

    /// <summary>Setup is executing now.</summary>
    Running = 3,

    /// <summary>Everything the platform needs is in place.</summary>
    Ready = 4,

    /// <summary>Setup ran and did not finish.</summary>
    Failed = 5,

    /// <summary>Setup completed but something needs a person to look at it.</summary>
    NeedsAttention = 6,
}

/// <summary>The state of one setup step.</summary>
public enum SetupStepState
{
    /// <summary>Unreadable or unrecognised. Shown as unknown, never as done.</summary>
    Unknown = 0,

    /// <summary>Not begun.</summary>
    Pending = 1,

    /// <summary>In progress.</summary>
    Running = 2,

    /// <summary>Done.</summary>
    Ready = 3,

    /// <summary>Failed.</summary>
    Failed = 4,

    /// <summary>Done, but a person should look at it.</summary>
    NeedsAttention = 5,

    /// <summary>Deliberately not applicable on this node.</summary>
    Skipped = 6,
}

/// <summary>One row of the setup report.</summary>
/// <param name="Id">Stable identifier, e.g. <c>database</c>.</param>
/// <param name="Label">Human name, e.g. "Database".</param>
/// <param name="State">Where that step got to.</param>
/// <param name="Detail">The host's plain-English explanation. May be empty.</param>
public sealed record SetupStep(string Id, string Label, SetupStepState State, string Detail);

/// <summary>Whether the shell got an answer from <c>/host/setup</c> at all.</summary>
public enum SetupAvailability
{
    /// <summary>Not asked yet.</summary>
    NotChecked = 0,

    /// <summary>Read successfully.</summary>
    Available = 1,

    /// <summary>
    /// The host answered 404. It predates the setup endpoint — an old build,
    /// not a broken one.
    /// </summary>
    NotSupportedByHost = 2,

    /// <summary>The host answered, but not with something this shell could read.</summary>
    Unreadable = 3,

    /// <summary>The request did not complete. Usually the gateway is down.</summary>
    Unreachable = 4,
}

/// <summary>
/// What <c>GET /host/setup</c> last said.
/// </summary>
/// <remarks>
/// The reader below is deliberately liberal about field names and strict about
/// meanings. It will accept several plausible spellings of "state" and "detail",
/// because this shell and that endpoint are built by different hands; but any
/// state word it does not recognise becomes <see cref="SetupState.Unknown"/>
/// rather than being guessed at, so a rename on the host side can make the
/// shell say "I don't know" but can never make it say "ready" about a platform
/// that is not.
/// </remarks>
public sealed record SetupSnapshot
{
    public static readonly SetupSnapshot NotChecked = new()
    {
        Availability = SetupAvailability.NotChecked,
        State = SetupState.Unknown,
    };

    public required SetupAvailability Availability { get; init; }

    public required SetupState State { get; init; }

    /// <summary>The state word exactly as the host sent it, for the diagnostic.</summary>
    public string? RawState { get; init; }

    public IReadOnlyList<SetupStep> Steps { get; init; } = Array.Empty<SetupStep>();

    /// <summary>Why the snapshot is not <see cref="SetupAvailability.Available"/>.</summary>
    public string? Problem { get; init; }

    /// <summary>The host's own one-line summary, when it sent one.</summary>
    public string? Summary { get; init; }

    /// <summary>True only when the platform positively said it is set up.</summary>
    public bool IsReady => Availability == SetupAvailability.Available && State == SetupState.Ready;

    /// <summary>True while setup is actively doing something.</summary>
    public bool IsBusy => Availability == SetupAvailability.Available
        && State is SetupState.Running or SetupState.Checking;

    /// <summary>True when a person has to act.</summary>
    public bool NeedsOperator => Availability == SetupAvailability.Available
        && State is SetupState.NotStarted or SetupState.Failed or SetupState.NeedsAttention;

    /// <summary>The host does not have the endpoint at all.</summary>
    public bool HostTooOld => Availability == SetupAvailability.NotSupportedByHost;

    public static SetupSnapshot Absent(string hostVersionHint) => new()
    {
        Availability = SetupAvailability.NotSupportedByHost,
        State = SetupState.Unknown,
        Problem =
            "This gateway does not report first-run setup state: it answered 404 for /host/setup, "
            + "which means it is older than the build this shell expects"
            + (string.IsNullOrWhiteSpace(hostVersionHint) ? "." : $" ({hostVersionHint}).")
            + " The platform may be perfectly set up — this shell simply cannot check from here. "
            + "Update the platform to see setup state, or verify it from the console.",
    };

    public static SetupSnapshot Unreachable(string problem) => new()
    {
        Availability = SetupAvailability.Unreachable,
        State = SetupState.Unknown,
        Problem = string.IsNullOrWhiteSpace(problem)
            ? "The shell could not ask the gateway about setup."
            : problem,
    };

    public static SetupSnapshot Unreadable(string problem) => new()
    {
        Availability = SetupAvailability.Unreadable,
        State = SetupState.Unknown,
        Problem = string.IsNullOrWhiteSpace(problem)
            ? "The gateway's setup report could not be read."
            : problem,
    };
}

/// <summary>Parses the body of <c>GET /host/setup</c>.</summary>
public static class SetupSnapshotReader
{
    /// <summary>
    /// Step identifiers this shell knows about, in the order they are shown.
    /// A step the host reports that is not on this list is still displayed,
    /// after these — a new setup step must never be invisible just because this
    /// shell has not been rebuilt.
    /// </summary>
    private static readonly string[] KnownOrder =
    {
        "database",
        "schema",
        "assets",
        "points",
        "bindings",
        "alarm_definitions",
        "load_schedule",
    };

    /// <summary>Reads a setup body. Returns a snapshot in every case.</summary>
    public static SetupSnapshot Read(string? json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            return SetupSnapshot.Unreadable("The gateway's setup report was empty.");
        }

        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(json!);
        }
        catch (JsonException ex)
        {
            return SetupSnapshot.Unreadable($"The gateway's setup report was not valid JSON: {ex.Message}");
        }

        using (document)
        {
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                return SetupSnapshot.Unreadable("The gateway's setup report was not a JSON object.");
            }

            var raw = FirstString(root, "state", "status", "overall", "overallState");
            if (raw is null)
            {
                return SetupSnapshot.Unreadable(
                    "The gateway's setup report did not say what state setup is in.");
            }

            var steps = ReadSteps(root);

            return new SetupSnapshot
            {
                Availability = SetupAvailability.Available,
                State = ParseState(raw),
                RawState = raw,
                Steps = steps,
                Summary = FirstString(root, "summary", "detail", "message", "description"),
                Problem = null,
            };
        }
    }

    /// <summary>Maps a state word. Anything unrecognised is Unknown, never Ready.</summary>
    public static SetupState ParseState(string? value) => Normalize(value) switch
    {
        "not_started" or "notstarted" or "none" => SetupState.NotStarted,
        "checking" or "check" => SetupState.Checking,
        "running" or "in_progress" or "inprogress" => SetupState.Running,
        "ready" or "complete" or "completed" or "done" or "ok" => SetupState.Ready,
        "failed" or "error" or "fault" => SetupState.Failed,
        "needs_attention" or "needsattention" or "attention" or "warning" or "degraded"
            => SetupState.NeedsAttention,
        _ => SetupState.Unknown,
    };

    /// <summary>Maps a step state word. Anything unrecognised is Unknown, never Ready.</summary>
    public static SetupStepState ParseStepState(string? value) => Normalize(value) switch
    {
        "pending" or "not_started" or "notstarted" or "waiting" or "queued" => SetupStepState.Pending,
        "running" or "in_progress" or "inprogress" or "checking" => SetupStepState.Running,
        "ready" or "ok" or "complete" or "completed" or "done" or "present" => SetupStepState.Ready,
        "failed" or "error" or "fault" => SetupStepState.Failed,
        "needs_attention" or "needsattention" or "attention" or "warning" or "degraded"
            => SetupStepState.NeedsAttention,
        "skipped" or "not_applicable" or "notapplicable" or "n/a" => SetupStepState.Skipped,
        _ => SetupStepState.Unknown,
    };

    private static IReadOnlyList<SetupStep> ReadSteps(JsonElement root)
    {
        var found = new List<SetupStep>();

        if (TryGet(root, out var element, "steps", "checks", "detail", "details", "items"))
        {
            switch (element.ValueKind)
            {
                case JsonValueKind.Array:
                    foreach (var item in element.EnumerateArray())
                    {
                        var step = ReadStep(item, keyFallback: null);
                        if (step is not null)
                        {
                            found.Add(step);
                        }
                    }
                    break;

                case JsonValueKind.Object:
                    // The map form: { "database": { "state": ..., "detail": ... } }
                    foreach (var property in element.EnumerateObject())
                    {
                        var step = ReadStep(property.Value, property.Name);
                        if (step is not null)
                        {
                            found.Add(step);
                        }
                    }
                    break;

                default:
                    break;
            }
        }

        return Order(found);
    }

    private static SetupStep? ReadStep(JsonElement element, string? keyFallback)
    {
        if (element.ValueKind == JsonValueKind.String && keyFallback is not null)
        {
            // { "database": "ready" } — terse but legal, and readable.
            return new SetupStep(
                keyFallback,
                Humanize(keyFallback),
                ParseStepState(element.GetString()),
                string.Empty);
        }

        if (element.ValueKind != JsonValueKind.Object)
        {
            return null;
        }

        var id = FirstString(element, "id", "key", "step", "name") ?? keyFallback;
        if (id is null)
        {
            return null;
        }

        // A label the host wrote is always preferred over one this shell
        // derives: the host knows what it is actually checking.
        var label = FirstString(element, "label", "title", "displayName", "name") ?? Humanize(id);

        return new SetupStep(
            id,
            label,
            ParseStepState(FirstString(element, "state", "status", "result")),
            FirstString(element, "detail", "explanation", "message", "description", "reason")
                ?? string.Empty);
    }

    /// <summary>
    /// Known steps first, in their documented order, then anything else in the
    /// order the host sent it.
    /// </summary>
    private static IReadOnlyList<SetupStep> Order(List<SetupStep> steps)
    {
        if (steps.Count == 0)
        {
            return Array.Empty<SetupStep>();
        }

        return steps
            .Select((step, index) => (step, index))
            .OrderBy(pair => RankOf(pair.step.Id))
            .ThenBy(pair => pair.index)
            .Select(pair => pair.step)
            .ToList();
    }

    private static int RankOf(string id)
    {
        var normalized = Normalize(id);
        for (var i = 0; i < KnownOrder.Length; i++)
        {
            if (string.Equals(KnownOrder[i], normalized, StringComparison.Ordinal))
            {
                return i;
            }
        }

        return KnownOrder.Length;
    }

    /// <summary>"alarm_definitions" and "alarmDefinitions" both become "alarm_definitions".</summary>
    private static string Normalize(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return string.Empty;
        }

        var text = value.Trim();
        var builder = new System.Text.StringBuilder(text.Length + 4);

        for (var i = 0; i < text.Length; i++)
        {
            var c = text[i];
            if (c is ' ' or '-' or '_')
            {
                // Collapse runs of separators: "Not-Started" and "not_started"
                // have to land on the same word, and doubling up here is how
                // "Not-Started" quietly failed to match anything.
                if (builder.Length > 0 && builder[^1] != '_')
                {
                    builder.Append('_');
                }

                continue;
            }

            // A capital starts a new word only after a lower-case letter or a
            // digit — so "alarmDefinitions" splits but "HTTPRoutes" does not
            // fragment into single letters.
            if (char.IsAsciiLetterUpper(c)
                && builder.Length > 0
                && builder[^1] != '_'
                && !char.IsAsciiLetterUpper(text[i - 1]))
            {
                builder.Append('_');
            }

            builder.Append(char.ToLowerInvariant(c));
        }

        return builder.ToString().Trim('_');
    }

    /// <summary>"alarm_definitions" becomes "Alarm definitions".</summary>
    internal static string Humanize(string id)
    {
        var normalized = Normalize(id).Replace('_', ' ');
        if (normalized.Length == 0)
        {
            return id;
        }

        return char.ToUpperInvariant(normalized[0]) + normalized[1..];
    }

    private static bool TryGet(JsonElement root, out JsonElement value, params string[] names)
    {
        foreach (var name in names)
        {
            if (root.TryGetProperty(name, out value))
            {
                return true;
            }
        }

        value = default;
        return false;
    }

    private static string? FirstString(JsonElement root, params string[] names)
    {
        if (root.ValueKind != JsonValueKind.Object)
        {
            return null;
        }

        foreach (var name in names)
        {
            if (root.TryGetProperty(name, out var element)
                && element.ValueKind == JsonValueKind.String)
            {
                var text = element.GetString();
                if (!string.IsNullOrWhiteSpace(text))
                {
                    return text!.Trim();
                }
            }
        }

        return null;
    }
}
