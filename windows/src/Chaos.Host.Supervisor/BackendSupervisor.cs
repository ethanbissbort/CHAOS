using System.Globalization;
using Chaos.Host.Abstractions;
using Chaos.Host.Supervisor.Events;
using Chaos.Host.Supervisor.Health;
using Chaos.Host.Supervisor.Logging;
using Chaos.Host.Supervisor.Policy;
using Chaos.Host.Supervisor.Processes;
using Chaos.Host.Supervisor.Runtime;
using Chaos.Host.Supervisor.Time;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Supervisor;

/// <summary>Raised on every state change, so the gateway can react without polling.</summary>
public sealed class BackendStatusChangedEventArgs(BackendStatus previous, BackendSnapshot snapshot) : EventArgs
{
    public BackendStatus Previous { get; } = previous;

    public BackendSnapshot Snapshot { get; } = snapshot;

    public BackendStatus Status => Snapshot.Status;
}

/// <summary>
/// Runs the Python platform as a supervised child process of the gateway.
/// </summary>
/// <remarks>
/// <para>
/// The rule this class exists to enforce: <see cref="BackendStatus.Running"/>
/// is reported if and only if the platform's own <c>/health</c> endpoint
/// answered. A live PID proves the interpreter started, not that FastAPI
/// bound the port, not that the database opened, and not that the registry
/// loaded. Reporting a control plane as up because a process exists is exactly
/// how an operator ends up trusting a dashboard that is lying.
/// </para>
/// </remarks>
public sealed class BackendSupervisor : IBackendSupervisor, IBackendSupervisorDiagnostics, IAsyncDisposable
{
    private readonly BackendSupervisorOptions _options;
    private readonly IPythonRuntimeResolver _resolver;
    private readonly IProcessLauncher _launcher;
    private readonly IBackendHealthProbe _probe;
    private readonly IClock _clock;
    private readonly IChaosEventSink _events;
    private readonly ILogger<BackendSupervisor> _logger;
    private readonly RestartPolicy _policy;

    private readonly Lock _gate = new();
    private readonly Queue<string> _stderrTail = new();

    private BackendStatus _status = BackendStatus.Stopped;
    private string _statusDetail = "not started";
    private string? _failureReason;
    private int _restartCount;
    private int? _processId;
    private int? _lastExitCode;
    private DateTimeOffset? _lastExitAt;
    private string? _lastExitDetail;
    private DateTimeOffset? _runningSince;
    private DateTimeOffset? _lastHealthyAt;
    private string? _lastHealthDetail;
    private BackendRuntimeDescriptor? _runtime;
    private TimeSpan? _nextRestartDelay;
    private DateTimeOffset? _nextRestartAt;

    private Task? _loop;
    private CancellationTokenSource? _loopCancellation;
    private TaskCompletionSource<bool>? _firstOutcome;
    private bool _stopRequested;
    private bool _disposed;

    public BackendSupervisor(
        IOptions<BackendSupervisorOptions> options,
        IPythonRuntimeResolver resolver,
        IProcessLauncher launcher,
        IBackendHealthProbe probe,
        IClock clock,
        IChaosEventSink events,
        ILogger<BackendSupervisor> logger)
        : this(
            (options ?? throw new ArgumentNullException(nameof(options))).Value,
            resolver,
            launcher,
            probe,
            clock,
            events,
            logger,
            RandomJitterSource.Instance)
    {
    }

    public BackendSupervisor(
        BackendSupervisorOptions options,
        IPythonRuntimeResolver resolver,
        IProcessLauncher launcher,
        IBackendHealthProbe probe,
        IClock clock,
        IChaosEventSink events,
        ILogger<BackendSupervisor> logger,
        IJitterSource jitter)
    {
        _options = options ?? throw new ArgumentNullException(nameof(options));
        _resolver = resolver ?? throw new ArgumentNullException(nameof(resolver));
        _launcher = launcher ?? throw new ArgumentNullException(nameof(launcher));
        _probe = probe ?? throw new ArgumentNullException(nameof(probe));
        _clock = clock ?? throw new ArgumentNullException(nameof(clock));
        _events = events ?? throw new ArgumentNullException(nameof(events));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
        _policy = new RestartPolicy(_options, jitter ?? throw new ArgumentNullException(nameof(jitter)));
    }

    /// <inheritdoc />
    public BackendStatus Status
    {
        get
        {
            lock (_gate)
            {
                return _status;
            }
        }
    }

    /// <inheritdoc />
    public BackendSnapshot Snapshot
    {
        get
        {
            lock (_gate)
            {
                return BuildSnapshotLocked();
            }
        }
    }

    /// <summary>Fired after every status change, outside the internal lock.</summary>
    public event EventHandler<BackendStatusChangedEventArgs>? StatusChanged;

    // -- Start / Stop ------------------------------------------------------

    /// <inheritdoc />
    public async Task StartAsync(CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        _options.ThrowIfInvalid();

        Task<bool> firstOutcome;
        BackendStatus previous;
        BackendSnapshot snapshot;

        lock (_gate)
        {
            if (_loop is { IsCompleted: false })
            {
                // Idempotent: the host may call this and a hosted service may
                // call it too. Starting twice must not produce two backends.
                return;
            }

            _stopRequested = false;
            _policy.Reset();
            _loopCancellation?.Dispose();
            _loopCancellation = new CancellationTokenSource();
            _firstOutcome = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            firstOutcome = _firstOutcome.Task;

            previous = _status;
            ApplyStatusLocked(
                BackendStatus.Starting,
                $"supervision started; nothing has answered {_options.HealthEndpoint} yet",
                failureReason: null);
            snapshot = BuildSnapshotLocked();

            var token = _loopCancellation.Token;
            _loop = Task.Run(() => SupervisionLoopAsync(token), CancellationToken.None);
        }

        RaiseStatusChanged(previous, snapshot);

        _logger.LogInformation(
            "Backend supervision starting. Lifecycle events go to: {Sink}.",
            _events.Description);
        _events.Write(
            ChaosEventId.SupervisorStarting,
            ChaosEventLevel.Information,
            $"Project CHAOS backend supervision starting. Target {_options.BaseAddress}, " +
            $"readiness gated on {_options.HealthEndpoint}.");

        if (!_options.WaitForReadyOnStart)
        {
            return;
        }

        await WaitForFirstOutcomeAsync(firstOutcome, cancellationToken).ConfigureAwait(false);
    }

    private async Task WaitForFirstOutcomeAsync(Task<bool> firstOutcome, CancellationToken cancellationToken)
    {
        using var gateCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        var gate = _clock.Delay(_options.StartupGate, gateCancellation.Token);

        var completed = await Task.WhenAny(firstOutcome, gate).ConfigureAwait(false);
        await gateCancellation.CancelAsync().ConfigureAwait(false);

        if (completed != firstOutcome)
        {
            _logger.LogWarning(
                "The backend was not ready within the {Gate:0.#}s startup gate. The gateway will keep " +
                "running and answer 503 until {Health} responds; supervision continues in the background.",
                _options.StartupGate.TotalSeconds,
                _options.HealthEndpoint);
            return;
        }

        var ready = await firstOutcome.ConfigureAwait(false);
        if (!ready)
        {
            _logger.LogError(
                "The backend did not reach a healthy state. Status is {Status}: {Detail}",
                Status,
                Snapshot.StatusDetail);
        }
    }

    /// <inheritdoc />
    public async Task StopAsync(CancellationToken cancellationToken = default)
    {
        Task? loop;
        CancellationTokenSource? cancellation;

        lock (_gate)
        {
            _stopRequested = true;
            loop = _loop;
            cancellation = _loopCancellation;
        }

        if (loop is null || cancellation is null)
        {
            SetStatus(BackendStatus.Stopped, "stop requested; nothing was being supervised");
            return;
        }

        await cancellation.CancelAsync().ConfigureAwait(false);

        try
        {
            await loop.WaitAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            // The host gave up waiting. Never return leaving a child alive:
            // an orphaned uvicorn keeps the loopback port bound and the next
            // start fails with "address already in use" on a machine nobody
            // is sitting at.
            _logger.LogError(
                "Shutdown was cancelled before the backend finished stopping. The supervision loop " +
                "still holds the child and will terminate it; not waiting further.");
            _events.Write(
                ChaosEventId.ForcedTermination,
                ChaosEventLevel.Warning,
                "Host shutdown cancelled the backend stop; termination continues in the background.");
        }

        lock (_gate)
        {
            _loop = null;
        }

        if (Status != BackendStatus.Failed)
        {
            SetStatus(BackendStatus.Stopped, "stopped on request");
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;

        try
        {
            using var timeout = new CancellationTokenSource(_options.GracefulShutdownTimeout + TimeSpan.FromSeconds(10));
            await StopAsync(timeout.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            // StopAsync already logged and escalated.
        }

        lock (_gate)
        {
            _loopCancellation?.Dispose();
            _loopCancellation = null;
        }
    }

    // -- Supervision loop --------------------------------------------------

    private async Task SupervisionLoopAsync(CancellationToken cancellationToken)
    {
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                var outcome = await RunAttemptAsync(cancellationToken).ConfigureAwait(false);

                if (cancellationToken.IsCancellationRequested ||
                    StopRequested ||
                    outcome.Kind == RunOutcomeKind.StoppedOnRequest)
                {
                    break;
                }

                if (outcome.HealthyFor >= _options.HealthyRunDuration)
                {
                    _logger.LogInformation(
                        "The backend had been healthy for {Duration:0.#}s before this failure, so the " +
                        "failure history is forgiven and backoff starts again from the beginning.",
                        outcome.HealthyFor.TotalSeconds);
                    PolicyRecordHealthyRun();
                }

                var decision = PolicyRecordFailure();

                if (!decision.ShouldRetry)
                {
                    var reason = $"{decision.Reason} Last failure: {outcome.Detail}";
                    SetFailed(reason);
                    _events.Write(ChaosEventId.CircuitBreakerTripped, ChaosEventLevel.Error, reason);
                    _logger.LogCritical(
                        "Backend supervision has given up. {Reason} The platform is DOWN and will not " +
                        "restart itself. Restart the CHAOS service once the cause is fixed.",
                        reason);
                    CompleteFirstOutcome(false);
                    return;
                }

                RecordRestartPending(decision.Delay);
                SetStatus(BackendStatus.Restarting, $"{outcome.Detail}; {decision.Reason}");
                _events.Write(
                    ChaosEventId.BackendRestarting,
                    ChaosEventLevel.Warning,
                    $"Backend restart scheduled. {outcome.Detail}; {decision.Reason}");

                try
                {
                    await _clock.Delay(decision.Delay, cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }

                ClearRestartPending();
            }

            if (Status != BackendStatus.Failed)
            {
                SetStatus(BackendStatus.Stopped, "supervision stopped");
            }

            _events.Write(
                ChaosEventId.SupervisorStopped,
                ChaosEventLevel.Information,
                "Project CHAOS backend supervision stopped.");
        }
        catch (Exception ex)
        {
            var reason = $"the supervision loop itself failed: {ex.GetType().Name}: {ex.Message}";
            SetFailed(reason);
            _logger.LogCritical(ex, "The backend supervision loop failed. The backend is not being supervised.");
            _events.Write(ChaosEventId.CircuitBreakerTripped, ChaosEventLevel.Error, reason);
        }
        finally
        {
            // If nothing ever became ready, unblock StartAsync with the truth.
            CompleteFirstOutcome(false);
        }
    }

    private async Task<RunOutcome> RunAttemptAsync(CancellationToken cancellationToken)
    {
        var resolution = _resolver.Resolve(_options);
        if (!resolution.Succeeded)
        {
            SetRuntime(null);
            _logger.LogError("No Python runtime could be resolved.{NewLine}{Report}", Environment.NewLine, resolution.ToReport());
            _events.Write(ChaosEventId.RuntimeUnresolvable, ChaosEventLevel.Error, resolution.ToReport());
            return RunOutcome.Failure(RunOutcomeKind.RuntimeUnavailable, resolution.Detail);
        }

        var runtime = resolution.Runtime!;
        if (SetRuntime(runtime))
        {
            _logger.LogInformation("Backend runtime resolved: {Description}", runtime.Description);
            _events.Write(
                ChaosEventId.RuntimeResolved,
                ChaosEventLevel.Information,
                $"Backend runtime: {runtime.Description}");
        }

        var spec = BuildStartSpec(runtime);
        ResetStandardErrorTail();

        ISupervisedProcess process;
        try
        {
            _logger.LogInformation(
                "Launching the platform backend: {CommandLine} (working directory {WorkingDirectory})",
                spec.ToCommandLine(),
                spec.WorkingDirectory);
            process = _launcher.Start(spec);
        }
        catch (Exception ex)
        {
            var detail = $"could not launch '{spec.FileName}': {ex.GetType().Name}: {ex.Message}";
            _logger.LogError(ex, "Launching the backend failed: {CommandLine}", spec.ToCommandLine());
            return RunOutcome.Failure(RunOutcomeKind.LaunchFailed, detail);
        }

        try
        {
            SetProcessId(process.Id);
            SetStatus(
                BackendStatus.Starting,
                $"pid {process.Id.ToString(CultureInfo.InvariantCulture)} launched; waiting for " +
                $"{_options.HealthEndpoint} to answer");
            _events.Write(
                ChaosEventId.BackendLaunched,
                ChaosEventLevel.Information,
                $"Backend process {process.Id.ToString(CultureInfo.InvariantCulture)} launched " +
                $"({runtime.Layout} runtime). Readiness is gated on {_options.HealthEndpoint}.");

            return await SuperviseProcessAsync(process, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            await StopChildAsync(process, "attempt finished").ConfigureAwait(false);
            SetProcessId(null);
            process.Dispose();
        }
    }

    private async Task<RunOutcome> SuperviseProcessAsync(ISupervisedProcess process, CancellationToken cancellationToken)
    {
        var readiness = await WaitForReadinessAsync(process, cancellationToken).ConfigureAwait(false);
        if (!readiness.IsReady)
        {
            if (readiness.Kind == RunOutcomeKind.ExitedDuringStartup)
            {
                RecordExit(readiness.ExitCode, readiness.Detail);
                _events.Write(ChaosEventId.BackendExited, ChaosEventLevel.Error, readiness.Detail);
            }
            else
            {
                _events.Write(ChaosEventId.BackendReadinessTimeout, ChaosEventLevel.Error, readiness.Detail);
            }

            return RunOutcome.Failure(readiness.Kind, readiness.Detail, readiness.ExitCode);
        }

        var readySince = _clock.UtcNow;
        MarkRunning(readiness.Detail, readySince);
        _events.Write(
            ChaosEventId.BackendReady,
            ChaosEventLevel.Information,
            $"Backend ready: {readiness.Detail}");
        CompleteFirstOutcome(true);

        var consecutiveUnhealthy = 0;

        // One exit task for the whole run. Creating it per iteration would
        // register a continuation and a cancellation callback every poll, and
        // this loop runs for weeks at a time.
        var exited = process.WaitForExitAsync(cancellationToken);

        while (true)
        {
            if (cancellationToken.IsCancellationRequested)
            {
                return RunOutcome.Stopped(_clock.UtcNow - readySince);
            }

            var tick = _clock.Delay(_options.HealthPollInterval, cancellationToken);

            var completed = await Task.WhenAny(exited, tick).ConfigureAwait(false);
            if (completed == exited)
            {
                int? code;
                try
                {
                    code = await exited.ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    return RunOutcome.Stopped(_clock.UtcNow - readySince);
                }

                var detail =
                    $"the backend exited with code {code?.ToString(CultureInfo.InvariantCulture) ?? "unknown"} " +
                    $"after {Describe(_clock.UtcNow - readySince)} of healthy service";
                RecordExit(code, detail);
                _events.Write(ChaosEventId.BackendExited, ChaosEventLevel.Error, detail);
                return RunOutcome.Failure(RunOutcomeKind.ExitedWhileRunning, detail, code, _clock.UtcNow - readySince);
            }

            if (cancellationToken.IsCancellationRequested)
            {
                return RunOutcome.Stopped(_clock.UtcNow - readySince);
            }

            BackendHealthResult health;
            try
            {
                health = await _probe
                    .CheckAsync(_options.HealthEndpoint, _options.HealthCheckTimeout, cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return RunOutcome.Stopped(_clock.UtcNow - readySince);
            }

            if (health.IsHealthy)
            {
                consecutiveUnhealthy = 0;
                RecordHealth(health, healthy: true);
                if (Status != BackendStatus.Running)
                {
                    MarkRunning(health.Detail, readySince);
                    _logger.LogInformation("The backend is answering /health again: {Detail}", health.Detail);
                }

                continue;
            }

            consecutiveUnhealthy++;
            RecordHealth(health, healthy: false);

            // A live process is not a healthy backend. Say what is actually
            // true: it is up, and it is not answering.
            SetStatus(
                BackendStatus.Starting,
                $"pid {process.Id.ToString(CultureInfo.InvariantCulture)} is alive but " +
                $"{_options.HealthEndpoint} is not answering " +
                $"({consecutiveUnhealthy.ToString(CultureInfo.InvariantCulture)} of " +
                $"{_options.UnhealthyProbeThreshold.ToString(CultureInfo.InvariantCulture)} failed probes): " +
                health.Detail);

            _logger.LogWarning(
                "Backend health probe failed ({Count}/{Threshold}): {Detail}",
                consecutiveUnhealthy,
                _options.UnhealthyProbeThreshold,
                health.Detail);

            if (consecutiveUnhealthy >= _options.UnhealthyProbeThreshold)
            {
                var detail =
                    $"the process is running but {_options.HealthEndpoint} failed " +
                    $"{consecutiveUnhealthy.ToString(CultureInfo.InvariantCulture)} consecutive probes: {health.Detail}";
                _events.Write(ChaosEventId.BackendUnhealthy, ChaosEventLevel.Error, detail);
                return RunOutcome.Failure(RunOutcomeKind.Unhealthy, detail, null, _clock.UtcNow - readySince);
            }
        }
    }

    private async Task<ReadinessResult> WaitForReadinessAsync(ISupervisedProcess process, CancellationToken cancellationToken)
    {
        var deadline = _clock.UtcNow + _options.ReadinessTimeout;
        var probes = 0;
        var lastDetail = "no health probe has completed yet";
        var exited = process.WaitForExitAsync(cancellationToken);

        while (true)
        {
            if (cancellationToken.IsCancellationRequested)
            {
                return ReadinessResult.NotReady(RunOutcomeKind.StoppedOnRequest, "stop requested during startup");
            }

            if (process.HasExited)
            {
                var code = process.ExitCode;
                return ReadinessResult.NotReady(
                    RunOutcomeKind.ExitedDuringStartup,
                    $"the backend exited with code {code?.ToString(CultureInfo.InvariantCulture) ?? "unknown"} " +
                    $"before answering {_options.HealthEndpoint}. Last stderr: {LastStandardErrorLine()}",
                    code);
            }

            BackendHealthResult health;
            try
            {
                health = await _probe
                    .CheckAsync(_options.HealthEndpoint, _options.HealthCheckTimeout, cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return ReadinessResult.NotReady(RunOutcomeKind.StoppedOnRequest, "stop requested during startup");
            }

            probes++;
            RecordHealth(health, health.IsHealthy);
            lastDetail = health.Detail;

            if (health.IsHealthy)
            {
                return ReadinessResult.Ready(health.Detail);
            }

            if (_clock.UtcNow >= deadline)
            {
                return ReadinessResult.NotReady(
                    RunOutcomeKind.ReadinessTimeout,
                    $"the backend did not answer {_options.HealthEndpoint} within " +
                    $"{Describe(_options.ReadinessTimeout)} ({probes.ToString(CultureInfo.InvariantCulture)} probes). " +
                    $"Last result: {lastDetail}. Last stderr: {LastStandardErrorLine()}");
            }

            var tick = _clock.Delay(_options.HealthPollInterval, cancellationToken);
            await Task.WhenAny(exited, tick).ConfigureAwait(false);
        }
    }

    /// <summary>
    /// Ask, wait, then kill. The wait is bounded because a control system that
    /// hangs on shutdown never comes back up either.
    /// </summary>
    private async Task StopChildAsync(ISupervisedProcess process, string why)
    {
        if (process.HasExited)
        {
            return;
        }

        _logger.LogInformation(
            "Stopping the backend (pid {Pid}): {Why}",
            process.Id,
            why);

        bool asked;
        string signalDetail;
        try
        {
            asked = process.TryRequestGracefulShutdown(out signalDetail);
        }
        catch (Exception ex)
        {
            asked = false;
            signalDetail = $"{ex.GetType().Name}: {ex.Message}";
        }

        if (asked)
        {
            _logger.LogInformation("Asked the backend to stop: {Detail}", signalDetail);
        }
        else
        {
            _logger.LogWarning(
                "No graceful stop channel to the backend ({Detail}). Waiting {Timeout:0.#}s, then killing " +
                "the process tree.",
                signalDetail,
                _options.GracefulShutdownTimeout.TotalSeconds);
        }

        var exited = process.WaitForExitAsync(CancellationToken.None);
        var timeout = _clock.Delay(_options.GracefulShutdownTimeout, CancellationToken.None);

        if (await Task.WhenAny(exited, timeout).ConfigureAwait(false) == exited)
        {
            int? code;
            try
            {
                code = await exited.ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                // Losing the exit code must not turn a clean stop into a failure.
                _logger.LogWarning(ex, "The backend exited but its exit code could not be read.");
                code = null;
            }

            _logger.LogInformation("The backend exited cleanly with code {ExitCode}.", code);
            RecordExit(
                code,
                $"stopped on request; exit code {code?.ToString(CultureInfo.InvariantCulture) ?? "unknown"}");
            return;
        }

        process.Kill(out var killDetail);
        var message =
            $"The backend did not exit within {Describe(_options.GracefulShutdownTimeout)} of the stop " +
            $"request, so it was terminated: {killDetail}";
        _logger.LogWarning("{Message}", message);
        _events.Write(ChaosEventId.ForcedTermination, ChaosEventLevel.Warning, message);

        // Bounded final wait so we do not report "stopped" while it is dying.
        var settle = _clock.Delay(TimeSpan.FromSeconds(5), CancellationToken.None);
        await Task.WhenAny(exited, settle).ConfigureAwait(false);
        RecordExit(process.ExitCode, message);
    }

    // -- Child process specification --------------------------------------

    internal ProcessStartSpec BuildStartSpec(BackendRuntimeDescriptor runtime)
    {
        var arguments = new List<string>(runtime.BaseArguments)
        {
            // Launch the platform through its own CLI rather than reimplementing
            // its startup. `--log-level` is a GLOBAL flag and must precede the
            // subcommand (src/homestead_twin/cli.py: build_parser).
            "-m",
            "homestead_twin.cli",
            "--log-level",
            _options.LogLevel,
            "serve",
            "--host",
            _options.BindAddress,
            "--port",
            _options.Port.ToString(CultureInfo.InvariantCulture),
        };

        arguments.AddRange(_options.ExtraServeArguments);

        var environment = new Dictionary<string, string?>(runtime.Environment, StringComparer.Ordinal)
        {
            // Without this, CPython block-buffers stdout when it is a pipe and
            // the platform's logs arrive minutes late or not at all.
            ["PYTHONUNBUFFERED"] = "1",
            ["PYTHONIOENCODING"] = "utf-8",
            // A stray .pyc write into a read-only install directory is not
            // worth failing a start over.
            ["PYTHONDONTWRITEBYTECODE"] = "1",
        };

        if (_options.DatabaseUrl is { Length: > 0 } databaseUrl)
        {
            environment["HOMESTEAD_DATABASE_URL"] = databaseUrl;
        }

        if (_options.DataDirectory is { Length: > 0 } dataDirectory)
        {
            environment["HOMESTEAD_DATA_DIR"] = dataDirectory;
        }

        foreach (var (key, value) in _options.Environment)
        {
            environment[key] = value;
        }

        return new ProcessStartSpec
        {
            FileName = runtime.Executable,
            Arguments = arguments,
            WorkingDirectory = runtime.WorkingDirectory,
            Environment = environment,
            OnOutput = HandleOutput,
        };
    }

    private void HandleOutput(ProcessOutputLine line)
    {
        var parsed = BackendLogLineParser.Parse(line);

        _logger.Log(
            parsed.Level,
            "[backend:{Stream}] {Logger} {Message}",
            line.Stream == ProcessOutputStream.StandardError ? "stderr" : "stdout",
            parsed.Logger ?? "-",
            parsed.Message);

        if (line.Stream != ProcessOutputStream.StandardError)
        {
            return;
        }

        lock (_gate)
        {
            _stderrTail.Enqueue(line.Text);
            while (_stderrTail.Count > _options.StandardErrorTailLines)
            {
                _stderrTail.Dequeue();
            }
        }
    }

    // -- State bookkeeping -------------------------------------------------

    private void SetStatus(BackendStatus status, string detail)
    {
        BackendStatus previous;
        BackendSnapshot snapshot;

        lock (_gate)
        {
            previous = _status;
            ApplyStatusLocked(status, detail, failureReason: null);
            snapshot = BuildSnapshotLocked();
        }

        RaiseStatusChanged(previous, snapshot);
    }

    private void SetFailed(string reason)
    {
        BackendStatus previous;
        BackendSnapshot snapshot;

        lock (_gate)
        {
            previous = _status;
            ApplyStatusLocked(BackendStatus.Failed, reason, reason);
            _runningSince = null;
            _nextRestartDelay = null;
            _nextRestartAt = null;
            snapshot = BuildSnapshotLocked();
        }

        RaiseStatusChanged(previous, snapshot);
    }

    private void MarkRunning(string detail, DateTimeOffset readySince)
    {
        BackendStatus previous;
        BackendSnapshot snapshot;

        lock (_gate)
        {
            previous = _status;
            _runningSince ??= readySince;
            _nextRestartDelay = null;
            _nextRestartAt = null;
            ApplyStatusLocked(BackendStatus.Running, detail, failureReason: null);
            snapshot = BuildSnapshotLocked();
        }

        RaiseStatusChanged(previous, snapshot);
    }

    private void ApplyStatusLocked(BackendStatus status, string detail, string? failureReason)
    {
        _status = status;
        _statusDetail = detail;
        _failureReason = failureReason;

        if (status is BackendStatus.Stopped or BackendStatus.Failed or BackendStatus.Restarting)
        {
            _runningSince = null;
        }
    }

    private void RaiseStatusChanged(BackendStatus previous, BackendSnapshot snapshot)
    {
        if (previous != snapshot.Status)
        {
            _logger.LogInformation(
                "Backend status {Previous} -> {Status}: {Detail}",
                previous,
                snapshot.Status,
                snapshot.StatusDetail);
        }

        var handler = StatusChanged;
        if (handler is null)
        {
            return;
        }

        try
        {
            handler(this, new BackendStatusChangedEventArgs(previous, snapshot));
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "A backend status subscriber threw. Supervision is unaffected.");
        }
    }

    private bool StopRequested
    {
        get
        {
            lock (_gate)
            {
                return _stopRequested;
            }
        }
    }

    // The policy owns mutable failure history, so every touch shares the
    // supervisor's lock: the loop writes it while Snapshot reads it.
    private RestartDecision PolicyRecordFailure()
    {
        lock (_gate)
        {
            return _policy.RecordFailure(_clock.UtcNow);
        }
    }

    private void PolicyRecordHealthyRun()
    {
        lock (_gate)
        {
            _policy.RecordHealthyRun();
        }
    }

    private bool SetRuntime(BackendRuntimeDescriptor? runtime)
    {
        lock (_gate)
        {
            var changed = _runtime?.Description != runtime?.Description;
            _runtime = runtime;
            return changed;
        }
    }

    private void SetProcessId(int? processId)
    {
        lock (_gate)
        {
            _processId = processId;
        }
    }

    private void RecordExit(int? exitCode, string detail)
    {
        lock (_gate)
        {
            _lastExitCode = exitCode;
            _lastExitAt = _clock.UtcNow;
            _lastExitDetail = detail;
            _runningSince = null;
        }
    }

    private void RecordHealth(BackendHealthResult health, bool healthy)
    {
        lock (_gate)
        {
            _lastHealthDetail = health.Detail;
            if (healthy)
            {
                _lastHealthyAt = _clock.UtcNow;
            }
        }
    }

    private void RecordRestartPending(TimeSpan delay)
    {
        lock (_gate)
        {
            _restartCount++;
            _nextRestartDelay = delay;
            _nextRestartAt = _clock.UtcNow + delay;
        }
    }

    private void ClearRestartPending()
    {
        lock (_gate)
        {
            _nextRestartDelay = null;
            _nextRestartAt = null;
        }
    }

    private void ResetStandardErrorTail()
    {
        lock (_gate)
        {
            _stderrTail.Clear();
        }
    }

    private string LastStandardErrorLine()
    {
        lock (_gate)
        {
            return _stderrTail.Count == 0 ? "(the backend wrote nothing to stderr)" : _stderrTail.Last();
        }
    }

    private void CompleteFirstOutcome(bool ready)
    {
        TaskCompletionSource<bool>? source;
        lock (_gate)
        {
            source = _firstOutcome;
        }

        source?.TrySetResult(ready);
    }

    private BackendSnapshot BuildSnapshotLocked() => new()
    {
        Status = _status,
        StatusDetail = _statusDetail,
        FailureReason = _failureReason,
        RestartCount = _restartCount,
        ConsecutiveFailures = _policy.ConsecutiveFailures,
        FailuresInBreakerWindow = _policy.FailuresInWindow,
        ProcessId = _processId,
        LastExitCode = _lastExitCode,
        LastExitAt = _lastExitAt,
        LastExitDetail = _lastExitDetail,
        StandardErrorTail = [.. _stderrTail],
        RunningSince = _runningSince,
        Uptime = _runningSince is null ? null : _clock.UtcNow - _runningSince.Value,
        LastHealthyAt = _lastHealthyAt,
        LastHealthDetail = _lastHealthDetail,
        RuntimeLayout = _runtime?.Layout ?? BackendRuntimeLayout.Unresolved,
        RuntimeDescription = _runtime?.Description,
        HealthEndpoint = _options.HealthEndpoint,
        NextRestartDelay = _nextRestartDelay,
        NextRestartAt = _nextRestartAt,
        EventSinkDescription = _events.Description,
    };

    private static string Describe(TimeSpan value) =>
        value.TotalSeconds < 90
            ? value.TotalSeconds.ToString("0.#", CultureInfo.InvariantCulture) + "s"
            : value.TotalMinutes.ToString("0.#", CultureInfo.InvariantCulture) + "m";

    // -- Internal result types --------------------------------------------

    private enum RunOutcomeKind
    {
        /// <summary>Placeholder for a result that carries no failure.</summary>
        None,
        StoppedOnRequest,
        RuntimeUnavailable,
        LaunchFailed,
        ExitedDuringStartup,
        ReadinessTimeout,
        ExitedWhileRunning,
        Unhealthy,
    }

    private readonly record struct RunOutcome(
        RunOutcomeKind Kind,
        string Detail,
        int? ExitCode,
        TimeSpan HealthyFor)
    {
        public static RunOutcome Failure(RunOutcomeKind kind, string detail, int? exitCode = null, TimeSpan? healthyFor = null) =>
            new(kind, detail, exitCode, healthyFor ?? TimeSpan.Zero);

        public static RunOutcome Stopped(TimeSpan healthyFor) =>
            new(RunOutcomeKind.StoppedOnRequest, "stop requested", null, healthyFor);
    }

    private readonly record struct ReadinessResult(bool IsReady, RunOutcomeKind Kind, string Detail, int? ExitCode)
    {
        public static ReadinessResult Ready(string detail) =>
            new(true, RunOutcomeKind.None, detail, null);

        public static ReadinessResult NotReady(RunOutcomeKind kind, string detail, int? exitCode = null) =>
            new(false, kind, detail, exitCode);
    }
}
