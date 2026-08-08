namespace Chaos.Host.Setup;

/// <summary>
/// Overall state of first-run platform setup, as reported by
/// <c>GET /host/setup</c>.
/// </summary>
/// <remarks>
/// <para>
/// The vocabulary follows the rule the rest of Project CHAOS follows: unknown
/// reads as unknown. There is no value here that means "probably fine".
/// <see cref="Ready"/> is only ever reported after the platform's own
/// <c>status --json</c> confirmed a complete schema and a loaded registry.
/// </para>
/// <para>
/// Wire values are lower snake case: <c>not_started</c>, <c>checking</c>,
/// <c>running</c>, <c>ready</c>, <c>failed</c>, <c>needs_attention</c>.
/// </para>
/// </remarks>
public enum SetupState
{
    /// <summary>
    /// Nothing has been checked or done. The honest initial value, and the
    /// steady state on a node where <c>Chaos:AutoSetup</c> is off and no
    /// operator has pressed anything. It is not a claim that setup is needed,
    /// nor that it is not.
    /// </summary>
    NotStarted = 0,

    /// <summary>
    /// Reading the current state of the database — schema, registry counts,
    /// design package. No writes happen in this state.
    /// </summary>
    Checking = 1,

    /// <summary>
    /// Setup is under way: the gateway is running the platform's own
    /// <c>init-db</c> and <c>load-all --skip-missing</c> as child processes.
    /// </summary>
    Running = 2,

    /// <summary>
    /// The database has every declared table and the design package is loaded.
    /// The only state that means "the platform can serve".
    /// </summary>
    Ready = 3,

    /// <summary>
    /// A setup step ran and failed, or the state could not be determined at
    /// all. The failure carries the command, the exit code, the real error and
    /// the log path — see the <c>failure</c> object on <c>GET /host/setup</c>.
    /// </summary>
    Failed = 4,

    /// <summary>
    /// The database exists but does not look like something this gateway should
    /// touch unattended: a partial schema over populated tables, or a partially
    /// loaded registry. Nothing has been changed and nothing will be, because
    /// this database may hold a homestead's alarm and telemetry history.
    /// An operator can still force a (non-destructive) run.
    /// </summary>
    NeedsAttention = 5,
}

/// <summary>
/// State of one setup step — one row of the checklist on
/// <c>GET /host/setup</c>.
/// </summary>
/// <remarks>
/// Wire values are lower snake case: <c>unknown</c>, <c>absent</c>,
/// <c>partial</c>, <c>present</c>, <c>running</c>, <c>failed</c>.
/// </remarks>
public enum SetupStepState
{
    /// <summary>
    /// Not determined. This is never a claim that the thing is missing, and
    /// never a claim that it is there.
    /// </summary>
    Unknown = 0,

    /// <summary>Checked, and definitely not there.</summary>
    Absent = 1,

    /// <summary>Checked, and there but incomplete or inconsistent.</summary>
    Partial = 2,

    /// <summary>Checked, and there.</summary>
    Present = 3,

    /// <summary>Being created or loaded right now.</summary>
    Running = 4,

    /// <summary>This step ran and failed.</summary>
    Failed = 5,
}

/// <summary>
/// Wire values and plain-English explanations for the setup vocabulary.
/// </summary>
/// <remarks>
/// Kept in one place so the words an operator reads on the shell, in the log
/// and in the API are literally the same words.
/// </remarks>
internal static class SetupVocabulary
{
    /// <summary>The wire value for an overall state.</summary>
    public static string Wire(this SetupState state) => state switch
    {
        SetupState.NotStarted => "not_started",
        SetupState.Checking => "checking",
        SetupState.Running => "running",
        SetupState.Ready => "ready",
        SetupState.Failed => "failed",
        SetupState.NeedsAttention => "needs_attention",
        _ => "unknown",
    };

    /// <summary>What an overall state means, in one sentence.</summary>
    public static string Explain(this SetupState state) => state switch
    {
        SetupState.NotStarted =>
            "Setup has not been checked or run on this host. This says nothing about whether the platform "
          + "database exists.",
        SetupState.Checking =>
            "Reading the platform's own view of its database. Nothing is being written.",
        SetupState.Running =>
            "Creating the database schema and loading the design package by running the platform's own CLI.",
        SetupState.Ready =>
            "The database has every declared table and the design package is loaded. Nothing to do.",
        SetupState.Failed =>
            "A setup step failed, or the current state could not be determined. See 'failure' for the command, "
          + "the exit code and the log.",
        SetupState.NeedsAttention =>
            "The database exists but does not look like a database this gateway should modify unattended. "
          + "Nothing has been changed. An operator has to decide what happens next.",
        _ => "Unknown state.",
    };

    /// <summary>The wire value for a step state.</summary>
    public static string Wire(this SetupStepState state) => state switch
    {
        SetupStepState.Unknown => "unknown",
        SetupStepState.Absent => "absent",
        SetupStepState.Partial => "partial",
        SetupStepState.Present => "present",
        SetupStepState.Running => "running",
        SetupStepState.Failed => "failed",
        _ => "unknown",
    };

    /// <summary>What a step state means, in one sentence.</summary>
    public static string Explain(this SetupStepState state) => state switch
    {
        SetupStepState.Unknown => "Not determined. Not a claim that it is missing.",
        SetupStepState.Absent => "Checked, and not there.",
        SetupStepState.Partial => "Checked, and there but incomplete.",
        SetupStepState.Present => "Checked, and there.",
        SetupStepState.Running => "Being created or loaded now.",
        SetupStepState.Failed => "This step ran and failed.",
        _ => "Not determined.",
    };
}
