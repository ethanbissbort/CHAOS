namespace Chaos.Host.Setup;

/// <summary>
/// The stable identifiers of the setup steps reported on
/// <c>GET /host/setup</c>. The shell renders rows by id, so these are a
/// contract: add ids, never rename them.
/// </summary>
internal static class SetupStepIds
{
    public const string DataDirectory = "dataDirectory";
    public const string Database = "database";
    public const string Schema = "schema";
    public const string Assets = "assets";
    public const string Points = "points";
    public const string Bindings = "bindings";
    public const string AlarmDefinitions = "alarmDefinitions";
    public const string LoadSchedule = "loadSchedule";
    public const string DesignPackage = "designPackage";

    /// <summary>Every id, in the order the shell should render them.</summary>
    public static readonly IReadOnlyList<string> Ordered =
    [
        DataDirectory,
        Database,
        Schema,
        Assets,
        Points,
        Bindings,
        AlarmDefinitions,
        LoadSchedule,
        DesignPackage,
    ];
}

/// <summary>One row of the setup checklist.</summary>
/// <param name="Id">Stable identifier — see <see cref="SetupStepIds"/>.</param>
/// <param name="Title">Short human label.</param>
/// <param name="State">This step's state.</param>
/// <param name="Detail">Plain English: what was found and what it means. Always populated.</param>
/// <param name="Count">
/// A row count where one applies, or null where it does not apply or is not
/// known. Null is never rendered as zero: "no rows" and "could not count" are
/// different findings.
/// </param>
/// <param name="Value">
/// The thing itself where there is one — a path, a URL — or null.
/// </param>
internal sealed record SetupStepReport(
    string Id,
    string Title,
    SetupStepState State,
    string Detail,
    int? Count = null,
    string? Value = null);

/// <summary>How a platform command ended.</summary>
internal enum PlatformCommandOutcome
{
    /// <summary>Exit code 0.</summary>
    Succeeded = 0,

    /// <summary>The process ran and exited non-zero.</summary>
    Failed = 1,

    /// <summary>The process did not finish inside its budget and was killed.</summary>
    TimedOut = 2,

    /// <summary>No process was ever started — no interpreter, no platform source, launch refused.</summary>
    CouldNotStart = 3,
}

/// <summary>One command this gateway ran, as an operator would see it.</summary>
/// <param name="Command">The command line, quoted as an operator would type it.</param>
/// <param name="Purpose">Why the gateway ran it.</param>
/// <param name="StartedUtc">When it started.</param>
/// <param name="DurationSeconds">How long it took, or null if it never started.</param>
/// <param name="ExitCode">The exit code, or null if the process never ran or was killed.</param>
/// <param name="Outcome">The outcome.</param>
/// <param name="Error">The failure, if any. Null on success.</param>
internal sealed record SetupCommandReport(
    string Command,
    string Purpose,
    DateTimeOffset StartedUtc,
    double? DurationSeconds,
    int? ExitCode,
    PlatformCommandOutcome Outcome,
    string? Error);

/// <summary>
/// Everything an operator needs to act on a setup failure without reading a
/// stack trace.
/// </summary>
/// <param name="Step">Which step failed — a <see cref="SetupStepIds"/> value, or <c>"probe"</c>.</param>
/// <param name="Message">The actual error, in the platform's own words where it produced one.</param>
/// <param name="Command">The command that failed, or null when nothing was launched.</param>
/// <param name="WorkingDirectory">Where it ran, or null.</param>
/// <param name="ExitCode">Its exit code, or null when it never ran or was killed.</param>
/// <param name="StandardErrorTail">The last lines the command wrote to stderr. Empty when there were none.</param>
/// <param name="LogPath">Where the full transcript is, or null when no log could be written.</param>
/// <param name="OccurredUtc">When it failed.</param>
/// <param name="Remedy">What to do about it, in one or two sentences.</param>
internal sealed record SetupFailureReport(
    string Step,
    string Message,
    string? Command,
    string? WorkingDirectory,
    int? ExitCode,
    IReadOnlyList<string> StandardErrorTail,
    string? LogPath,
    DateTimeOffset OccurredUtc,
    string Remedy);

/// <summary>
/// An immutable reading of setup state. Published by
/// <see cref="PlatformSetupCoordinator"/> and rendered by
/// <c>GET /host/setup</c> and <c>GET /health</c>.
/// </summary>
/// <remarks>
/// A single volatile reference to an immutable record, so readers never see a
/// half-written state and never block. Every field that could be unknown is
/// nullable and stays null rather than being defaulted to something that reads
/// as healthy.
/// </remarks>
internal sealed record SetupSnapshot
{
    /// <summary>Overall state.</summary>
    public SetupState State { get; init; } = SetupState.NotStarted;

    /// <summary>One line naming the situation. Always populated.</summary>
    public required string Summary { get; init; }

    /// <summary>Whether the automatic run at startup is enabled on this node.</summary>
    public bool AutoSetupEnabled { get; init; }

    /// <summary>What setup is doing right now, or null when it is not doing anything.</summary>
    public string? CurrentActivity { get; init; }

    /// <summary>When <see cref="CurrentActivity"/> started, or null.</summary>
    public DateTimeOffset? CurrentActivityStartedUtc { get; init; }

    /// <summary>The last thing setup finished doing, or null if it has never done anything.</summary>
    public string? LastActivity { get; init; }

    /// <summary>When <see cref="LastActivity"/> finished, or null.</summary>
    public DateTimeOffset? LastActivityUtc { get; init; }

    /// <summary>When the current or most recent run started, or null.</summary>
    public DateTimeOffset? StartedUtc { get; init; }

    /// <summary>When the most recent run finished, or null while one is in flight or none has run.</summary>
    public DateTimeOffset? CompletedUtc { get; init; }

    /// <summary>When the database was last read, or null if it never has been.</summary>
    public DateTimeOffset? LastCheckedUtc { get; init; }

    /// <summary>How many runs this host has started since it came up.</summary>
    public int RunCount { get; init; }

    /// <summary>What started the most recent run — <c>startup</c> or <c>api</c> — or null.</summary>
    public string? Trigger { get; init; }

    /// <summary>The checklist. Empty until the first check completes.</summary>
    public IReadOnlyList<SetupStepReport> Steps { get; init; } = [];

    /// <summary>The failure, when <see cref="State"/> is <see cref="SetupState.Failed"/>. Otherwise null.</summary>
    public SetupFailureReport? Failure { get; init; }

    /// <summary>Every command this host ran, oldest first, most recent last.</summary>
    public IReadOnlyList<SetupCommandReport> Commands { get; init; } = [];

    /// <summary>How the Python runtime resolved, or null if it has not been resolved yet.</summary>
    public string? RuntimeDescription { get; init; }

    /// <summary>The database the platform reported, redacted, or null if it has not said.</summary>
    public string? DatabaseUrl { get; init; }

    /// <summary>The design-package directory in use, or null if it is not known.</summary>
    public string? DataDirectory { get; init; }

    /// <summary>
    /// How <see cref="DataDirectory"/> was arrived at: <c>configured</c>,
    /// <c>derived</c> or <c>unknown</c>.
    /// </summary>
    public string DataDirectorySource { get; init; } = "unknown";

    /// <summary>Where the setup transcript is written, or null when no log could be opened.</summary>
    public string? LogPath { get; init; }

    /// <summary>Why no log could be opened, or null when there is no problem.</summary>
    public string? LogProblem { get; init; }

    /// <summary>The starting snapshot for a host that has not checked anything yet.</summary>
    /// <param name="autoSetupEnabled">Whether the automatic run is enabled.</param>
    /// <returns>The snapshot.</returns>
    public static SetupSnapshot Initial(bool autoSetupEnabled) => new()
    {
        State = SetupState.NotStarted,
        AutoSetupEnabled = autoSetupEnabled,
        Summary = autoSetupEnabled
            ? "Setup has not run yet on this host."
            : "Automatic setup is turned off on this node (Chaos:AutoSetup=false), so nothing has been checked "
            + "or changed. POST /host/setup/run?force=true to check and set up anyway.",
    };

    /// <summary>Appends a command record, keeping the most recent <paramref name="keep"/>.</summary>
    /// <param name="command">The record to append.</param>
    /// <param name="keep">How many to keep.</param>
    /// <returns>A new snapshot.</returns>
    public SetupSnapshot WithCommand(SetupCommandReport command, int keep = 24)
    {
        var appended = new List<SetupCommandReport>(Commands) { command };
        if (appended.Count > keep)
        {
            appended.RemoveRange(0, appended.Count - keep);
        }

        return this with { Commands = appended };
    }
}
