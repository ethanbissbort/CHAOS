namespace Chaos.Shell.Core;

/// <summary>One rendered setup row.</summary>
/// <param name="Label">Step name.</param>
/// <param name="State">Drawing state, shared with the launcher's own checks.</param>
/// <param name="Detail">
/// The host's explanation, or this shell's wording for a step that said nothing.
/// Never empty: a row with a state and no words is a row an operator cannot act
/// on.
/// </param>
public sealed record SetupRow(string Label, CheckState State, string Detail);

/// <summary>
/// The setup panel, ready to bind: a headline, a sentence, rows, and the label
/// for the button that does something about it.
/// </summary>
/// <param name="Headline">Short state, e.g. "Setup has not run".</param>
/// <param name="Summary">One sentence an operator can act on.</param>
/// <param name="Rows">Per-step detail. Empty when the host reported none.</param>
/// <param name="OverallState">Drawing state for the launcher's setup check.</param>
/// <param name="RunLabel">
/// What the run/retry button should say, or null when running setup is not an
/// available action at all.
/// </param>
/// <param name="RunIsPrimary">Whether that button is the main thing to press.</param>
public sealed record SetupView(
    string Headline,
    string Summary,
    IReadOnlyList<SetupRow> Rows,
    CheckState OverallState,
    string? RunLabel,
    bool RunIsPrimary)
{
    /// <summary>Rows that are failed or need attention, for a compact summary.</summary>
    public IReadOnlyList<SetupRow> Problems =>
        Rows.Where(r => r.State is CheckState.Fail or CheckState.Warn).ToList();
}

/// <summary>Turns a <see cref="SetupSnapshot"/> into something a page can draw.</summary>
public static class SetupPresenter
{
    /// <summary>Label used when setup has never run.</summary>
    public const string SetUpNow = "Set up now";

    /// <summary>Label used when setup has run and did not finish cleanly.</summary>
    public const string RetrySetup = "Retry setup";

    /// <summary>Label used when setup is complete and the operator wants it again.</summary>
    public const string RunAgain = "Run setup checks again";

    public static SetupView Present(SetupSnapshot snapshot)
    {
        ArgumentNullException.ThrowIfNull(snapshot);

        var rows = snapshot.Steps.Select(ToRow).ToList();

        return snapshot.Availability switch
        {
            SetupAvailability.NotChecked => new SetupView(
                "Not checked yet",
                "The shell has not asked the platform about first-run setup yet.",
                rows,
                CheckState.Unknown,
                RunLabel: null,
                RunIsPrimary: false),

            SetupAvailability.Unreachable => new SetupView(
                "Unknown",
                snapshot.Problem
                    ?? "The shell could not ask the platform about setup, because it cannot reach it.",
                rows,
                CheckState.Unknown,
                RunLabel: null,
                RunIsPrimary: false),

            SetupAvailability.NotSupportedByHost => new SetupView(
                "Not reported by this platform",
                snapshot.Problem ?? "This gateway does not report first-run setup state.",
                rows,
                CheckState.Unknown,
                RunLabel: null,
                RunIsPrimary: false),

            SetupAvailability.Unreadable => new SetupView(
                "Unreadable",
                snapshot.Problem
                    ?? "The platform answered about setup, but not in a form this shell understands.",
                rows,
                CheckState.Unknown,
                RunLabel: null,
                RunIsPrimary: false),

            _ => Available(snapshot, rows),
        };
    }

    private static SetupView Available(SetupSnapshot snapshot, IReadOnlyList<SetupRow> rows)
    {
        var problems = rows.Where(r => r.State is CheckState.Fail or CheckState.Warn).ToList();
        var because = problems.Count == 0
            ? string.Empty
            : " " + string.Join(" ", problems.Select(p => $"{p.Label}: {p.Detail}"));

        return snapshot.State switch
        {
            SetupState.Ready => new SetupView(
                "Set up",
                snapshot.Summary
                    ?? "The platform reports that everything it needs is in place: database, points, "
                       + "bindings, alarm definitions and the load schedule.",
                rows,
                CheckState.Pass,
                RunAgain,
                RunIsPrimary: false),

            SetupState.NotStarted => new SetupView(
                "Not set up yet",
                snapshot.Summary
                    ?? "This platform has never been set up. It needs its database created and its "
                       + "points, bindings, alarm definitions and load schedule loaded before it can "
                       + "control anything.",
                rows,
                CheckState.Fail,
                SetUpNow,
                RunIsPrimary: true),

            SetupState.Checking => new SetupView(
                "Checking",
                snapshot.Summary ?? "The platform is working out what it still needs.",
                rows,
                CheckState.Checking,
                RunLabel: null,
                RunIsPrimary: false),

            SetupState.Running => new SetupView(
                "Setting up",
                snapshot.Summary
                    ?? "Setup is running. This can take a few minutes the first time, while the "
                       + "database is created and the site's points are loaded.",
                rows,
                CheckState.Checking,
                RunLabel: null,
                RunIsPrimary: false),

            SetupState.Failed => new SetupView(
                "Setup failed",
                (snapshot.Summary ?? "Setup ran and did not finish.") + because,
                rows,
                CheckState.Fail,
                RetrySetup,
                RunIsPrimary: true),

            SetupState.NeedsAttention => new SetupView(
                "Setup needs attention",
                (snapshot.Summary ?? "Setup finished, but something needs a person to look at it.")
                    + because,
                rows,
                CheckState.Warn,
                RetrySetup,
                RunIsPrimary: true),

            _ => new SetupView(
                "Unknown",
                $"The platform reported a setup state this shell does not recognise"
                    + (snapshot.RawState is { Length: > 0 } ? $" ('{snapshot.RawState}')" : string.Empty)
                    + ". The shell will not guess whether that means it is ready.",
                rows,
                CheckState.Unknown,
                RunLabel: null,
                RunIsPrimary: false),
        };
    }

    private static SetupRow ToRow(SetupStep step) => new(
        step.Label,
        step.State switch
        {
            SetupStepState.Ready => CheckState.Pass,
            SetupStepState.Skipped => CheckState.NotApplicable,
            SetupStepState.Running => CheckState.Checking,
            SetupStepState.Pending => CheckState.Pending,
            SetupStepState.Failed => CheckState.Fail,
            SetupStepState.NeedsAttention => CheckState.Warn,
            _ => CheckState.Unknown,
        },
        string.IsNullOrWhiteSpace(step.Detail)
            ? DefaultDetail(step.State)
            : step.Detail.Trim());

    private static string DefaultDetail(SetupStepState state) => state switch
    {
        SetupStepState.Ready => "Done. The platform did not say more.",
        SetupStepState.Pending => "Not started yet.",
        SetupStepState.Running => "In progress.",
        SetupStepState.Failed => "Failed. The platform did not say why — check the platform log.",
        SetupStepState.NeedsAttention =>
            "Needs attention. The platform did not say why — check the platform log.",
        SetupStepState.Skipped => "Not applicable on this node.",
        _ => "The platform reported a state this shell does not recognise.",
    };
}
