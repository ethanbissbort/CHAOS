using Chaos.Host.Configuration;

namespace Chaos.Host.Setup;

/// <summary>
/// Builds the bodies of <c>GET /host/setup</c> and <c>POST /host/setup/run</c>,
/// and the <c>setup</c> object embedded in <c>GET /health</c>.
/// </summary>
/// <remarks>
/// The shape is a contract: the WinUI shell renders it. Fields are added, never
/// renamed or repurposed. Anything unknown is a JSON <c>null</c> or the string
/// <c>"unknown"</c> — never a zero or a default that reads as healthy.
/// </remarks>
internal static class SetupPayload
{
    /// <summary>The full report.</summary>
    /// <param name="snapshot">The current reading.</param>
    /// <param name="options">Gateway options, for the configuration echo.</param>
    /// <param name="recordPath">Where the setup state file lives, or null.</param>
    /// <param name="nowUtc">The current time.</param>
    /// <returns>An object ready for <c>Results.Json</c>.</returns>
    public static object Create(
        SetupSnapshot snapshot,
        ChaosHostOptions options,
        string? recordPath,
        DateTimeOffset nowUtc)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        ArgumentNullException.ThrowIfNull(options);

        return new
        {
            state = snapshot.State.Wire(),
            stateExplanation = snapshot.State.Explain(),
            summary = snapshot.Summary,
            generatedUtc = nowUtc,

            // Decisions the shell needs to render a button without re-deriving
            // them from the state machine.
            autoSetupEnabled = snapshot.AutoSetupEnabled,
            inProgress = snapshot.State is SetupState.Checking or SetupState.Running,
            setupRequired = snapshot.State is SetupState.Failed or SetupState.NeedsAttention
                         || (snapshot.State == SetupState.NotStarted && !snapshot.AutoSetupEnabled),
            runRequiresForce = !snapshot.AutoSetupEnabled || snapshot.State == SetupState.NeedsAttention,

            currentActivity = snapshot.CurrentActivity,
            currentActivityStartedUtc = snapshot.CurrentActivityStartedUtc,
            lastActivity = snapshot.LastActivity,
            lastActivityUtc = snapshot.LastActivityUtc,

            startedUtc = snapshot.StartedUtc,
            completedUtc = snapshot.CompletedUtc,
            lastCheckedUtc = snapshot.LastCheckedUtc,
            durationSeconds = Duration(snapshot, nowUtc),
            runCount = snapshot.RunCount,
            trigger = snapshot.Trigger,

            steps = snapshot.Steps.Select(step => new
            {
                id = step.Id,
                title = step.Title,
                state = step.State.Wire(),
                stateExplanation = step.State.Explain(),
                detail = step.Detail,
                count = step.Count,
                value = step.Value,
            }),

            failure = snapshot.Failure is null ? null : new
            {
                step = snapshot.Failure.Step,
                message = snapshot.Failure.Message,
                command = snapshot.Failure.Command,
                workingDirectory = snapshot.Failure.WorkingDirectory,
                exitCode = snapshot.Failure.ExitCode,
                standardErrorTail = snapshot.Failure.StandardErrorTail,
                logPath = snapshot.Failure.LogPath,
                occurredUtc = snapshot.Failure.OccurredUtc,
                remedy = snapshot.Failure.Remedy,
            },

            commands = snapshot.Commands.Select(command => new
            {
                command = command.Command,
                purpose = command.Purpose,
                startedUtc = command.StartedUtc,
                durationSeconds = command.DurationSeconds,
                exitCode = command.ExitCode,
                outcome = Outcome(command.Outcome),
                error = command.Error,
            }),

            configuration = new
            {
                autoSetup = options.AutoSetup,
                databaseUrl = snapshot.DatabaseUrl,
                databaseUrlSource = string.IsNullOrWhiteSpace(options.DatabaseUrl)
                    ? "platform default (Chaos:DatabaseUrl is not set)"
                    : "Chaos:DatabaseUrl",
                dataDirectory = snapshot.DataDirectory,
                dataDirectorySource = snapshot.DataDirectorySource,
                runtime = snapshot.RuntimeDescription,
                logPath = snapshot.LogPath,
                logProblem = snapshot.LogProblem,
                stateFile = recordPath,
                setupTimeoutSeconds = options.SetupTimeout.TotalSeconds,
                environmentVariables = new[]
                {
                    "CHAOS_AutoSetup", "CHAOS_DatabaseUrl", "CHAOS_DataDirectory",
                    "CHAOS_SetupLogPath", "CHAOS_SetupStateFile",
                },
            },

            actions = new
            {
                run = new
                {
                    method = "POST",
                    path = "/host/setup/run",
                    description = "Run setup now. Idempotent, and it never starts a second concurrent run. "
                                + "Add ?force=true to proceed when automatic setup is off or the database was "
                                + "assessed as needing attention; force never makes setup destructive.",
                },
            },

            promises = new[]
            {
                "Setup runs the platform's own CLI (init-db, load-all --skip-missing). It does not reimplement them.",
                "Nothing here drops, truncates or migrates. A database that looks wrong is reported, not repaired.",
                "'ready' is only reported after the platform's own status --json confirmed it.",
            },
        };
    }

    /// <summary>The compact form embedded in <c>GET /health</c>.</summary>
    /// <param name="snapshot">The current reading.</param>
    /// <returns>An object ready for <c>Results.Json</c>.</returns>
    public static object ForHealth(SetupSnapshot snapshot)
    {
        ArgumentNullException.ThrowIfNull(snapshot);

        return new
        {
            state = snapshot.State.Wire(),
            summary = snapshot.Summary,
            autoSetupEnabled = snapshot.AutoSetupEnabled,
            currentActivity = snapshot.CurrentActivity,
            lastCheckedUtc = snapshot.LastCheckedUtc,
            failure = snapshot.Failure?.Message,
            detail = "/host/setup",
        };
    }

    /// <summary>The body of <c>POST /host/setup/run</c>.</summary>
    /// <param name="acceptance">Whether the request started a run.</param>
    /// <param name="forced">Whether the caller asked to override the gateway's own guards.</param>
    /// <param name="snapshot">The reading as it stood when the request was answered.</param>
    /// <param name="options">Gateway options.</param>
    /// <param name="recordPath">Where the setup state file lives, or null.</param>
    /// <param name="nowUtc">The current time.</param>
    /// <returns>An object ready for <c>Results.Json</c>.</returns>
    public static object ForRun(
        SetupRunAcceptance acceptance,
        bool forced,
        SetupSnapshot snapshot,
        ChaosHostOptions options,
        string? recordPath,
        DateTimeOffset nowUtc)
    {
        ArgumentNullException.ThrowIfNull(acceptance);

        return new
        {
            accepted = acceptance.Accepted,
            reason = acceptance.Reason,
            detail = acceptance.Detail,
            forced,
            setup = Create(snapshot, options, recordPath, nowUtc),
        };
    }

    /// <summary>
    /// Whether the platform behind this gateway is known not to be serving
    /// because of setup, and how to describe that on <c>/health</c>.
    /// </summary>
    /// <param name="state">The setup state.</param>
    /// <returns>
    /// <c>"degraded"</c> or <c>"starting"</c> when setup should colour health,
    /// or null when it should not.
    /// </returns>
    /// <remarks>
    /// <see cref="SetupState.NotStarted"/> deliberately returns null. Nothing
    /// has been determined, and an undetermined check must not be reported as
    /// either a fault or an all-clear — the backend probe already says whether
    /// the platform is answering.
    /// </remarks>
    public static string? HealthContribution(SetupState state) => state switch
    {
        SetupState.Failed or SetupState.NeedsAttention => "degraded",
        SetupState.Checking or SetupState.Running => "starting",
        _ => null,
    };

    private static double? Duration(SetupSnapshot snapshot, DateTimeOffset nowUtc)
    {
        if (snapshot.StartedUtc is not { } started)
        {
            return null;
        }

        var end = snapshot.CompletedUtc ?? nowUtc;
        return Math.Round(Math.Max(0d, (end - started).TotalSeconds), 2);
    }

    private static string Outcome(PlatformCommandOutcome outcome) => outcome switch
    {
        PlatformCommandOutcome.Succeeded => "succeeded",
        PlatformCommandOutcome.Failed => "failed",
        PlatformCommandOutcome.TimedOut => "timed_out",
        PlatformCommandOutcome.CouldNotStart => "could_not_start",
        _ => "unknown",
    };
}
