using System.Globalization;
using Chaos.Host.Configuration;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Setup;

/// <summary>Whether a requested run was taken up, and why not if it was not.</summary>
/// <param name="Accepted">Whether a run was started by this request.</param>
/// <param name="Reason">A stable token: <c>started</c>, <c>already_running</c> or <c>auto_setup_disabled</c>.</param>
/// <param name="Detail">The same thing in operator English.</param>
internal sealed record SetupRunAcceptance(bool Accepted, string Reason, string Detail)
{
    /// <summary>A run was started.</summary>
    public const string Started = "started";

    /// <summary>A run was already in flight; this request did not start a second one.</summary>
    public const string AlreadyRunning = "already_running";

    /// <summary>Automatic setup is off on this node and the request did not ask to override that.</summary>
    public const string AutoSetupDisabled = "auto_setup_disabled";
}

/// <summary>
/// Performs first-run setup, and reports honestly on it.
/// </summary>
/// <remarks>
/// <para>
/// <b>What it does.</b> Runs the platform's own <c>status --json</c> to find out
/// where things stand, and, only when the database is genuinely empty, runs the
/// platform's own <c>init-db</c> and <c>load-all --skip-missing</c>. It never
/// reimplements either. Setup is complete when a second <c>status --json</c>
/// confirms it, not when a command exits zero.
/// </para>
/// <para>
/// <b>What it will not do.</b> It never drops, truncates or migrates anything.
/// The only two commands it can run create tables and upsert rows. A database
/// that is populated but not in the expected shape ends the run in
/// <see cref="SetupState.NeedsAttention"/> with an explanation, because this
/// platform holds a homestead's alarm and telemetry history and a wrong guess
/// here is unrecoverable.
/// </para>
/// <para>
/// <b>Concurrency.</b> One run at a time, enforced by a compare-and-swap. A
/// second request while one is in flight is declined with
/// <see cref="SetupRunAcceptance.AlreadyRunning"/> rather than queued, so the
/// shell's "Retry" button cannot start two imports.
/// </para>
/// </remarks>
internal sealed partial class PlatformSetupCoordinator : IDisposable
{
    private readonly IPlatformCommandRunner _runner;
    private readonly ChaosHostOptions _options;
    private readonly ISetupLog _log;
    private readonly ILogger<PlatformSetupCoordinator> _logger;
    private readonly CancellationTokenSource _lifetime = new();
    private readonly Lock _publishGate = new();
    private readonly string? _recordPath;

    private SetupSnapshot _snapshot;
    private int _running;
    private int _runsStarted;
    private Task _current = Task.CompletedTask;
    private bool _disposed;

    public PlatformSetupCoordinator(
        IPlatformCommandRunner runner,
        IOptions<ChaosHostOptions> options,
        ISetupLog log,
        ILogger<PlatformSetupCoordinator> logger)
    {
        ArgumentNullException.ThrowIfNull(options);

        _runner = runner;
        _options = options.Value;
        _log = log;
        _logger = logger;
        _recordPath = SetupRecord.ResolvePath(_options);
        _snapshot = SetupSnapshot.Initial(_options.AutoSetup) with
        {
            LogPath = log.Path,
            LogProblem = log.Problem,
            DataDirectory = string.IsNullOrWhiteSpace(_options.DataDirectory) ? null : _options.DataDirectory,
            DataDirectorySource = string.IsNullOrWhiteSpace(_options.DataDirectory) ? "unknown" : "configured",
            DatabaseUrl = string.IsNullOrWhiteSpace(_options.DatabaseUrl) ? null : _options.DatabaseUrl,
        };
    }

    /// <summary>The current reading. Never blocks, never throws.</summary>
    public SetupSnapshot Snapshot => Volatile.Read(ref _snapshot);

    /// <summary>How many runs this host has started. Used by tests to prove single-flight.</summary>
    public int RunsStarted => Volatile.Read(ref _runsStarted);

    /// <summary>The run in flight, or a completed task. Awaited by tests, never by a request.</summary>
    public Task Current => Volatile.Read(ref _current);

    /// <summary>Where the setup state file lives, or null when there is nowhere to put it.</summary>
    public string? RecordPath => _recordPath;

    /// <summary>
    /// Starts a setup attempt if one is not already in flight.
    /// </summary>
    /// <param name="trigger">What asked for it — <c>startup</c> or <c>api</c>.</param>
    /// <param name="force">
    /// Proceed even where the gateway would otherwise decline: automatic setup
    /// is off, or the database was assessed as needing attention. It never makes
    /// setup destructive — the same idempotent <c>init-db</c> and
    /// <c>load-all --skip-missing</c> run either way.
    /// </param>
    /// <returns>Whether a run was started.</returns>
    public SetupRunAcceptance RequestRun(string trigger, bool force)
    {
        if (!_options.AutoSetup && !force)
        {
            return new SetupRunAcceptance(false, SetupRunAcceptance.AutoSetupDisabled,
                "Automatic setup is turned off on this node (Chaos:AutoSetup=false), so the gateway will not "
              + "check or change the platform database. Repeat with ?force=true to run it anyway.");
        }

        if (Interlocked.CompareExchange(ref _running, 1, 0) != 0)
        {
            return new SetupRunAcceptance(false, SetupRunAcceptance.AlreadyRunning,
                "A setup run is already in progress. This request did not start a second one; poll "
              + "GET /host/setup for progress.");
        }

        Interlocked.Increment(ref _runsStarted);

        // Detached on purpose. Setting a homestead up takes tens of seconds, and
        // the shell has to be able to watch it happen rather than hold a socket
        // open through it.
        Volatile.Write(ref _current, Task.Run(() => RunGuardedAsync(trigger, force), CancellationToken.None));

        return new SetupRunAcceptance(true, SetupRunAcceptance.Started,
            force
                ? "Setup was started at an operator's explicit request. Poll GET /host/setup for progress."
                : "Setup was started. Poll GET /host/setup for progress.");
    }

    /// <summary>Cancels an in-flight run and stops accepting new ones.</summary>
    public void Shutdown()
    {
        if (!_disposed)
        {
            _lifetime.Cancel();
        }
    }

    /// <inheritdoc/>
    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        _lifetime.Cancel();
        _lifetime.Dispose();
    }

    private async Task RunGuardedAsync(string trigger, bool force)
    {
        try
        {
            await RunAsync(trigger, force).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            // The last line of defence. A background task that throws would
            // otherwise leave /host/setup frozen on "checking" forever, which is
            // the one thing this endpoint exists to prevent.
            LogUnexpected(ex);
            Publish(current => current with
            {
                State = SetupState.Failed,
                Summary = "Setup stopped on an unexpected error inside the gateway.",
                CurrentActivity = null,
                CurrentActivityStartedUtc = null,
                LastActivity = "Stopped on an unexpected error.",
                LastActivityUtc = DateTimeOffset.UtcNow,
                CompletedUtc = DateTimeOffset.UtcNow,
                Failure = new SetupFailureReport(
                    "gateway",
                    $"{ex.GetType().Name}: {ex.Message}",
                    Command: null,
                    WorkingDirectory: null,
                    ExitCode: null,
                    StandardErrorTail: [],
                    LogPath: _log.Path,
                    OccurredUtc: DateTimeOffset.UtcNow,
                    Remedy: "This is a fault in the gateway, not in the platform. Nothing was changed after the "
                          + "last command shown under 'commands'. Retry with POST /host/setup/run; if it repeats, "
                          + "the gateway log has the stack trace."),
            });
        }
        finally
        {
            Volatile.Write(ref _running, 0);
        }
    }

    private async Task RunAsync(string trigger, bool force)
    {
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(_lifetime.Token);
        budget.CancelAfter(_options.SetupTimeout);
        var token = budget.Token;

        var startedUtc = DateTimeOffset.UtcNow;
        _log.Write($"=== setup run: trigger={trigger} force={force.ToString(CultureInfo.InvariantCulture)} ===");
        LogRunStarted(trigger, force);

        Publish(current => current with
        {
            State = SetupState.Checking,
            Summary = "Reading the platform's own view of its database.",
            Trigger = trigger,
            StartedUtc = startedUtc,
            CompletedUtc = null,
            Failure = null,
            RunCount = current.RunCount + 1,
            CurrentActivity = "Checking the platform database (chaos status --json).",
            CurrentActivityStartedUtc = startedUtc,
            LogPath = _log.Path,
            LogProblem = _log.Problem,
        });

        var probe = await ProbeAsync(token).ConfigureAwait(false);
        if (probe.Assessment is null)
        {
            return;
        }

        var assessment = probe.Assessment;
        var data = probe.Data;

        switch (assessment.Verdict)
        {
            case SetupVerdict.Ready:
                Complete(SetupState.Ready, assessment,
                    "Checked; nothing to do. The platform was already set up.");
                return;

            case SetupVerdict.NeedsAttention when !force:
                Complete(SetupState.NeedsAttention, assessment,
                    "Checked; stopped without changing anything.");
                return;

            case SetupVerdict.Unknown:
                Complete(SetupState.NeedsAttention, assessment,
                    "Checked; the result was inconclusive.");
                return;

            default:
                break;
        }

        if (assessment.Verdict == SetupVerdict.NeedsAttention)
        {
            _log.Write("FORCED: proceeding against an operator's explicit request despite: " + assessment.Summary);
        }

        // --- init-db --------------------------------------------------------
        Publish(current => current with
        {
            State = SetupState.Running,
            Summary = "Setting the platform up.",
            Steps = MarkRunning(assessment.Steps, SetupStepIds.Schema, "Creating tables now."),
            CurrentActivity = "Creating the database schema (chaos init-db).",
            CurrentActivityStartedUtc = DateTimeOffset.UtcNow,
        });

        var initDb = await RunCommandAsync(
            ["init-db"],
            "Create every table the platform declares. Additive: create_all never alters or drops.",
            _options.SetupCommandTimeout,
            token).ConfigureAwait(false);

        if (!initDb.Succeeded)
        {
            Fail(SetupStepIds.Schema, initDb, assessment,
                "The platform could not create its tables. The message above is the platform's own. Most often "
              + "this is a database URL pointing somewhere unwritable, or a database user without CREATE rights. "
              + "Nothing was loaded. Fix the cause and press Retry.");
            return;
        }

        // --- load-all -------------------------------------------------------
        Publish(current => current with
        {
            Steps = MarkRunning(current.Steps, SetupStepIds.Assets, "Loading now."),
            CurrentActivity = "Loading the design package (chaos load-all --skip-missing).",
            CurrentActivityStartedUtc = DateTimeOffset.UtcNow,
            LastActivity = "Created the database schema.",
            LastActivityUtc = DateTimeOffset.UtcNow,
        });

        var loadAll = await RunCommandAsync(
            ["load-all", "--skip-missing"],
            "Load the registry, the EMS load schedule and the alarm definitions from data/. Upserts; never drops.",
            _options.SetupCommandTimeout,
            token).ConfigureAwait(false);

        if (!loadAll.Succeeded)
        {
            Fail(SetupStepIds.DesignPackage, loadAll, assessment,
                "The schema was created, but the design package did not load. The message above is the platform's "
              + "own. Run 'chaos validate' to check the design package against its schemas. Nothing was "
              + "dropped: each load stage commits separately and re-running is safe.");
            return;
        }

        // --- verify ---------------------------------------------------------
        Publish(current => current with
        {
            CurrentActivity = "Verifying the result (chaos status --json).",
            CurrentActivityStartedUtc = DateTimeOffset.UtcNow,
            LastActivity = "Loaded the design package.",
            LastActivityUtc = DateTimeOffset.UtcNow,
        });

        var verify = await ProbeAsync(token).ConfigureAwait(false);
        if (verify.Assessment is null)
        {
            return;
        }

        if (verify.Assessment.Verdict == SetupVerdict.Ready)
        {
            RecordSuccess(verify.Data, verify.Reading?.DatabaseUrl);
            Complete(SetupState.Ready, verify.Assessment, "Set the platform up and verified it.");
            LogRunReady();
            return;
        }

        Complete(SetupState.NeedsAttention, verify.Assessment,
            "Ran setup, but the database still does not look complete.");
    }

    private async Task<ProbeOutcome> ProbeAsync(CancellationToken cancellationToken)
    {
        var result = await RunCommandAsync(
            ["status", "--json"],
            "Ask the platform for its own account of its database. Read-only.",
            _options.SetupProbeTimeout,
            cancellationToken).ConfigureAwait(false);

        if (!result.Succeeded)
        {
            Fail("probe", result, assessment: null,
                result.Outcome == PlatformCommandOutcome.CouldNotStart
                    ? "The gateway could not run the platform CLI at all, so it does not know whether the "
                    + "database exists. Check that the Python runtime shipped with this install is intact, or "
                    + "set Chaos:Backend:PythonExecutable / Chaos:Backend:RepositoryRoot for a development "
                    + "checkout. Nothing was changed."
                    : "The gateway could not read the platform's state, so it will not change anything. The "
                    + "message above is the platform's own; fix the cause and press Retry.");
            return ProbeOutcome.Failed;
        }

        if (!PlatformStatusReading.TryParse(result.StandardOutput, out var reading, out var problem) || reading is null)
        {
            Fail("probe", result with { Error = problem }, assessment: null,
                "The platform CLI succeeded but the gateway could not understand its answer. This usually means "
              + "the gateway and the platform are different versions. Nothing was changed.");
            return ProbeOutcome.Failed;
        }

        var data = ResolveDesignPackage(result.WorkingDirectory);
        var database = DatabaseFileFacts.Inspect(reading.DatabaseUrl ?? NullIfBlank(_options.DatabaseUrl));
        var assessment = SetupAssessor.Assess(reading, data, database, SetupRecord.TryRead(_recordPath));

        Publish(current => current with
        {
            LastCheckedUtc = DateTimeOffset.UtcNow,
            Steps = assessment.Steps,
            DatabaseUrl = reading.DatabaseUrl ?? current.DatabaseUrl,
            DataDirectory = data.Directory ?? current.DataDirectory,
            DataDirectorySource = data.Source,
            RuntimeDescription = result.RuntimeDescription ?? current.RuntimeDescription,
        });

        return new ProbeOutcome(assessment, reading, data);
    }

    private async Task<PlatformCommandResult> RunCommandAsync(
        string[] arguments,
        string purpose,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        var result = await _runner.RunAsync(
            new PlatformCommandRequest { Arguments = arguments, Purpose = purpose, Timeout = timeout },
            cancellationToken).ConfigureAwait(false);

        Publish(current => current.WithCommand(result.ToReport(purpose)) with
        {
            RuntimeDescription = result.RuntimeDescription ?? current.RuntimeDescription,
        });

        return result;
    }

    /// <summary>
    /// Where <c>data/</c> is. Configured wins; otherwise it is derived from the
    /// working directory the runtime resolver chose, which is the repository
    /// root in a checkout and the app root in a packaged install — the two
    /// places the platform's own default puts it.
    /// </summary>
    private DesignPackageFacts ResolveDesignPackage(string? runtimeWorkingDirectory)
    {
        if (!string.IsNullOrWhiteSpace(_options.DataDirectory))
        {
            return DesignPackageFacts.Inspect(_options.DataDirectory, "configured");
        }

        if (string.IsNullOrWhiteSpace(runtimeWorkingDirectory))
        {
            return DesignPackageFacts.Unknown(
                "Chaos:DataDirectory is not set and the gateway has no working directory to derive data/ from, "
              + "so it does not know where the design package is. The platform still uses its own default.");
        }

        return DesignPackageFacts.Inspect(Path.Combine(runtimeWorkingDirectory, "data"), "derived");
    }

    private void RecordSuccess(DesignPackageFacts data, string? databaseUrl)
    {
        var problem = SetupRecord.TryWrite(_recordPath, new SetupRecord
        {
            CompletedUtc = DateTimeOffset.UtcNow,
            DesignPackageFingerprint = data.Fingerprint,
            DatabaseUrl = databaseUrl,
            HostVersion = HostVersion.Version,
        });

        if (problem is not null)
        {
            _log.Write("NOTE: " + problem);
            LogRecordNotWritten(problem);
        }
    }

    private void Complete(SetupState state, SetupAssessment assessment, string lastActivity)
    {
        var now = DateTimeOffset.UtcNow;
        _log.Write($"=== setup run finished: {state.Wire()} — {assessment.Summary} ===");

        Publish(current => current with
        {
            State = state,
            Summary = assessment.Summary,
            Steps = assessment.Steps,
            Failure = null,
            CurrentActivity = null,
            CurrentActivityStartedUtc = null,
            LastActivity = lastActivity,
            LastActivityUtc = now,
            CompletedUtc = now,
            LogPath = _log.Path,
            LogProblem = _log.Problem,
        });

        if (state == SetupState.NeedsAttention)
        {
            LogNeedsAttention(assessment.Summary);
        }
    }

    private void Fail(string step, PlatformCommandResult result, SetupAssessment? assessment, string remedy)
    {
        var now = DateTimeOffset.UtcNow;
        var message = result.Error ?? "The command failed without saying why.";

        _log.Write($"=== setup run failed at '{step}': {message} ===");
        LogRunFailed(step, result.CommandLine, message);

        Publish(current => current with
        {
            State = SetupState.Failed,
            Summary = $"Setup failed while running '{result.CommandLine}'.",
            Steps = MarkFailed(assessment?.Steps ?? current.Steps, step, message),
            CurrentActivity = null,
            CurrentActivityStartedUtc = null,
            LastActivity = $"Failed at '{step}'.",
            LastActivityUtc = now,
            CompletedUtc = now,
            LogPath = _log.Path,
            LogProblem = _log.Problem,
            Failure = new SetupFailureReport(
                step,
                message,
                result.CommandLine,
                result.WorkingDirectory,
                result.ExitCode,
                result.StandardErrorTail,
                _log.Path,
                now,
                remedy),
        });
    }

    private void Publish(Func<SetupSnapshot, SetupSnapshot> update)
    {
        lock (_publishGate)
        {
            Volatile.Write(ref _snapshot, update(_snapshot));
        }
    }

    private static IReadOnlyList<SetupStepReport> MarkRunning(
        IReadOnlyList<SetupStepReport> steps,
        string id,
        string detail) =>
        [.. steps.Select(step => step.Id == id
            ? step with { State = SetupStepState.Running, Detail = detail }
            : step)];

    private static IReadOnlyList<SetupStepReport> MarkFailed(
        IReadOnlyList<SetupStepReport> steps,
        string id,
        string detail) =>
        [.. steps.Select(step => step.Id == id
            ? step with { State = SetupStepState.Failed, Detail = detail }
            : step)];

    private static string? NullIfBlank(string? value) => string.IsNullOrWhiteSpace(value) ? null : value;

    private readonly record struct ProbeOutcome(
        SetupAssessment? Assessment,
        PlatformStatusReading? Reading,
        DesignPackageFacts Data)
    {
        public static ProbeOutcome Failed => new(null, null, DesignPackageFacts.Unknown("The probe failed."));
    }

    [LoggerMessage(EventId = 3201, Level = LogLevel.Information,
        Message = "First-run setup started (trigger {Trigger}, force {Force}). Progress is on GET /host/setup.")]
    private partial void LogRunStarted(string trigger, bool force);

    [LoggerMessage(EventId = 3202, Level = LogLevel.Information,
        Message = "First-run setup finished: the platform database is ready.")]
    private partial void LogRunReady();

    [LoggerMessage(EventId = 3203, Level = LogLevel.Error,
        Message = "First-run setup failed at '{Step}' running '{CommandLine}': {Message}")]
    private partial void LogRunFailed(string step, string commandLine, string message);

    [LoggerMessage(EventId = 3204, Level = LogLevel.Warning,
        Message = "First-run setup stopped without changing anything: {Summary}")]
    private partial void LogNeedsAttention(string summary);

    [LoggerMessage(EventId = 3205, Level = LogLevel.Warning,
        Message = "The setup state file could not be written: {Problem}")]
    private partial void LogRecordNotWritten(string problem);

    [LoggerMessage(EventId = 3206, Level = LogLevel.Error,
        Message = "First-run setup stopped on an unexpected error inside the gateway.")]
    private partial void LogUnexpected(Exception exception);
}
