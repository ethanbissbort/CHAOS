extern alias ChaosSupervisor;

using System.Diagnostics;
using System.Globalization;
using System.Text;
using Chaos.Host.Configuration;
using ChaosSupervisor::Chaos.Host.Supervisor;
using ChaosSupervisor::Chaos.Host.Supervisor.Processes;
using ChaosSupervisor::Chaos.Host.Supervisor.Runtime;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Setup;

/// <summary>
/// Runs the platform CLI through <c>Chaos.Host.Supervisor</c>'s process
/// abstractions.
/// </summary>
/// <remarks>
/// <para>
/// This is the only file in the gateway that touches the supervisor, and it
/// does so through an <c>extern alias</c> — see <c>Chaos.Host.csproj</c> for
/// why. It reuses <c>IPythonRuntimeResolver</c> and <c>IProcessLauncher</c>
/// rather than resolving an interpreter or spawning a process itself, so first
/// run setup and the supervised backend can never disagree about which Python
/// the platform runs on.
/// </para>
/// <para>
/// Failure is a value, not an exception. "No interpreter", "the CLI exited 1"
/// and "it hung and was killed" are three different findings an operator needs
/// to tell apart, and all three end up on <c>GET /host/setup</c>.
/// </para>
/// </remarks>
internal sealed partial class PlatformCommandRunner : IPlatformCommandRunner
{
    /// <summary>
    /// The platform CLI module, launched with <c>-m</c>. Matches what
    /// <c>BackendSupervisor.BuildStartSpec</c> launches, so setup and the
    /// backend run the same code.
    /// </summary>
    private const string CliModule = "chaos.cli";

    /// <summary>
    /// The platform's own environment variables, in its
    /// <c>CHAOS_&lt;SCREAMING_SNAKE_CASE&gt;</c> form.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <b>These share a prefix with the gateway's own settings and must not be
    /// confused with them.</b> <c>ChaosHostOptions</c> binds
    /// <c>CHAOS_&lt;PascalCase&gt;</c> — <c>CHAOS_DatabaseUrl</c>,
    /// <c>CHAOS_DataDirectory</c>, <c>CHAOS_AutoSetup</c>. The platform reads
    /// <c>CHAOS_&lt;SCREAMING_SNAKE_CASE&gt;</c> — <c>CHAOS_DATABASE_URL</c>,
    /// <c>CHAOS_DATA_DIR</c>. The underscores are what keep the two apart:
    /// neither binder's case-insensitive match can reach the other's names, so
    /// the same prefix over two casings is safe as long as no gateway option is
    /// ever named with an underscore. Do not add one.
    /// </para>
    /// <para>
    /// The child inherits the gateway's whole environment, so a
    /// <c>CHAOS_DATABASE_URL</c> already set for the service reaches the
    /// platform whether or not the gateway sets it. That is deliberate and
    /// harmless: the gateway only overrides when <c>Chaos:DatabaseUrl</c> is
    /// set, and otherwise reports whatever database the platform says it used.
    /// </para>
    /// </remarks>
    private const string DatabaseUrlVariable = "CHAOS_DATABASE_URL";

    /// <summary>The platform's design-package directory variable. See <see cref="DatabaseUrlVariable"/>.</summary>
    private const string DataDirectoryVariable = "CHAOS_DATA_DIR";

    /// <summary>Lines of child stderr kept for the operator snapshot.</summary>
    private const int StandardErrorTailLines = 40;

    /// <summary>Cap on captured stdout. <c>status --json</c> is a few KiB; a runaway must not eat memory.</summary>
    private const int StandardOutputCharacterLimit = 512 * 1024;

    private readonly IPythonRuntimeResolver _resolver;
    private readonly IProcessLauncher _launcher;
    private readonly BackendSupervisorOptions _runtime;
    private readonly ChaosHostOptions _options;
    private readonly ISetupLog _log;
    private readonly ILogger<PlatformCommandRunner> _logger;

    public PlatformCommandRunner(
        IPythonRuntimeResolver resolver,
        IProcessLauncher launcher,
        IOptions<BackendSupervisorOptions> runtimeOptions,
        IOptions<ChaosHostOptions> options,
        ISetupLog log,
        ILogger<PlatformCommandRunner> logger)
    {
        ArgumentNullException.ThrowIfNull(runtimeOptions);
        ArgumentNullException.ThrowIfNull(options);

        _resolver = resolver;
        _launcher = launcher;
        _runtime = runtimeOptions.Value;
        _options = options.Value;
        _log = log;
        _logger = logger;
    }

    /// <inheritdoc/>
    public async Task<PlatformCommandResult> RunAsync(
        PlatformCommandRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);

        var startedUtc = DateTimeOffset.UtcNow;
        var stopwatch = Stopwatch.StartNew();

        var resolution = _resolver.Resolve(_runtime);
        if (resolution.Runtime is null)
        {
            var report = resolution.ToReport();
            _log.Write($"CANNOT START: {string.Join(' ', request.Arguments)}{Environment.NewLine}{report}");
            LogRuntimeUnresolved(resolution.Detail);

            return new PlatformCommandResult
            {
                Outcome = PlatformCommandOutcome.CouldNotStart,
                CommandLine = "python -m " + CliModule + " " + string.Join(' ', request.Arguments),
                StartedUtc = startedUtc,
                Duration = stopwatch.Elapsed,
                Error = report,
            };
        }

        var descriptor = resolution.Runtime;
        var spec = BuildSpec(descriptor, request, out var capture);
        var commandLine = spec.ToCommandLine();

        _log.Write($"RUN  {commandLine}   (cwd {descriptor.WorkingDirectory}; {descriptor.Description})");
        LogRunning(commandLine, request.Purpose);

        ISupervisedProcess process;
        try
        {
            process = _launcher.Start(spec);
        }
        catch (Exception ex)
        {
            var error = $"Could not launch '{commandLine}': {ex.GetType().Name}: {ex.Message}";
            _log.Write("FAILED TO LAUNCH: " + error);
            LogLaunchFailed(ex, commandLine);

            return new PlatformCommandResult
            {
                Outcome = PlatformCommandOutcome.CouldNotStart,
                CommandLine = commandLine,
                WorkingDirectory = descriptor.WorkingDirectory,
                StartedUtc = startedUtc,
                Duration = stopwatch.Elapsed,
                Error = error,
                RuntimeDescription = descriptor.Description,
            };
        }

        using (process)
        {
            using var budget = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            budget.CancelAfter(request.Timeout);

            int exitCode;
            try
            {
                exitCode = await process.WaitForExitAsync(budget.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                process.Kill(out var killDetail);
                var cancelled = cancellationToken.IsCancellationRequested;
                var error = cancelled
                    ? $"The gateway is shutting down, so '{commandLine}' was stopped ({killDetail}). "
                    + "Nothing it had already committed is undone; the platform CLI commits each stage separately."
                    : string.Create(
                        CultureInfo.InvariantCulture,
                        $"'{commandLine}' did not finish within {request.Timeout.TotalSeconds:F0}s and was stopped ({killDetail}).");

                _log.Write("TIMED OUT: " + error);
                LogTimedOut(commandLine, request.Timeout);

                return new PlatformCommandResult
                {
                    Outcome = PlatformCommandOutcome.TimedOut,
                    CommandLine = commandLine,
                    WorkingDirectory = descriptor.WorkingDirectory,
                    StartedUtc = startedUtc,
                    Duration = stopwatch.Elapsed,
                    StandardOutput = capture.Output(),
                    StandardErrorTail = capture.ErrorTail(),
                    Error = error,
                    RuntimeDescription = descriptor.Description,
                };
            }

            var errorTail = capture.ErrorTail();
            _log.Write(string.Create(
                CultureInfo.InvariantCulture,
                $"EXIT {exitCode} after {stopwatch.Elapsed.TotalSeconds:F1}s   {commandLine}"));

            if (exitCode == 0)
            {
                LogSucceeded(commandLine, stopwatch.Elapsed);
                return new PlatformCommandResult
                {
                    Outcome = PlatformCommandOutcome.Succeeded,
                    CommandLine = commandLine,
                    WorkingDirectory = descriptor.WorkingDirectory,
                    ExitCode = 0,
                    StartedUtc = startedUtc,
                    Duration = stopwatch.Elapsed,
                    StandardOutput = capture.Output(),
                    StandardErrorTail = errorTail,
                    RuntimeDescription = descriptor.Description,
                };
            }

            LogFailed(commandLine, exitCode);
            return new PlatformCommandResult
            {
                Outcome = PlatformCommandOutcome.Failed,
                CommandLine = commandLine,
                WorkingDirectory = descriptor.WorkingDirectory,
                ExitCode = exitCode,
                StartedUtc = startedUtc,
                Duration = stopwatch.Elapsed,
                StandardOutput = capture.Output(),
                StandardErrorTail = errorTail,
                Error = DescribeExit(exitCode, errorTail),
                RuntimeDescription = descriptor.Description,
            };
        }
    }

    /// <summary>
    /// Turns the platform CLI's documented exit codes into a sentence, and
    /// prefers whatever the CLI itself printed over our paraphrase.
    /// </summary>
    private static string DescribeExit(int exitCode, IReadOnlyList<string> errorTail)
    {
        var meaning = exitCode switch
        {
            1 => "a runtime failure",
            2 => "a usage error — the gateway built an argument list the platform CLI did not accept",
            3 => "a subsystem was unavailable (that exit code means an importable module was missing)",
            _ => "an unrecognised failure",
        };

        var said = errorTail.Where(line => !string.IsNullOrWhiteSpace(line)).ToList();
        var reported = said.Count > 0
            ? string.Join(Environment.NewLine, said)
            : "The command printed nothing to stderr.";

        return string.Create(
            CultureInfo.InvariantCulture,
            $"The platform CLI exited {exitCode} ({meaning}).{Environment.NewLine}{reported}");
    }

    private ProcessStartSpec BuildSpec(
        BackendRuntimeDescriptor descriptor,
        PlatformCommandRequest request,
        out OutputCapture capture)
    {
        var capturing = new OutputCapture(_log);
        capture = capturing;

        var arguments = new List<string>(descriptor.BaseArguments) { "-m", CliModule };
        arguments.AddRange(request.Arguments);

        var environment = new Dictionary<string, string?>(descriptor.Environment, StringComparer.Ordinal)
        {
            // Unbuffered so the transcript is in order even if the CLI is
            // killed part way through; UTF-8 so an operator's site name does
            // not come back as mojibake on a Windows console codepage.
            ["PYTHONUNBUFFERED"] = "1",
            ["PYTHONIOENCODING"] = "utf-8",
        };

        var databaseUrl = FirstConfigured(_options.DatabaseUrl, _runtime.DatabaseUrl);
        if (databaseUrl is not null)
        {
            environment[DatabaseUrlVariable] = databaseUrl;
        }

        var dataDirectory = FirstConfigured(_options.DataDirectory, _runtime.DataDirectory);
        if (dataDirectory is not null)
        {
            environment[DataDirectoryVariable] = dataDirectory;
        }

        return new ProcessStartSpec
        {
            FileName = descriptor.Executable,
            Arguments = arguments,
            WorkingDirectory = descriptor.WorkingDirectory,
            Environment = environment,
            OnOutput = capturing.Append,
        };
    }

    /// <summary>
    /// The gateway's setting wins; the supervisor's is the fallback; neither
    /// set means "let the platform use its own default", which is more honest
    /// than the gateway guessing a path.
    /// </summary>
    private static string? FirstConfigured(string? preferred, string? fallback)
    {
        if (!string.IsNullOrWhiteSpace(preferred))
        {
            return preferred;
        }

        return string.IsNullOrWhiteSpace(fallback) ? null : fallback;
    }

    [LoggerMessage(EventId = 3101, Level = LogLevel.Information,
        Message = "Running the platform CLI: {CommandLine} ({Purpose})")]
    private partial void LogRunning(string commandLine, string purpose);

    [LoggerMessage(EventId = 3102, Level = LogLevel.Information,
        Message = "The platform CLI succeeded in {Elapsed}: {CommandLine}")]
    private partial void LogSucceeded(string commandLine, TimeSpan elapsed);

    [LoggerMessage(EventId = 3103, Level = LogLevel.Error,
        Message = "The platform CLI exited {ExitCode}: {CommandLine}. First-run setup did not complete; "
                + "GET /host/setup carries the error and the log path.")]
    private partial void LogFailed(string commandLine, int exitCode);

    [LoggerMessage(EventId = 3104, Level = LogLevel.Error,
        Message = "The platform CLI did not finish within {Timeout} and was stopped: {CommandLine}")]
    private partial void LogTimedOut(string commandLine, TimeSpan timeout);

    [LoggerMessage(EventId = 3105, Level = LogLevel.Error,
        Message = "No Python runtime could be resolved, so first-run setup cannot run: {Detail}")]
    private partial void LogRuntimeUnresolved(string detail);

    [LoggerMessage(EventId = 3106, Level = LogLevel.Error,
        Message = "The operating system refused to start the platform CLI: {CommandLine}")]
    private partial void LogLaunchFailed(Exception exception, string commandLine);

    /// <summary>
    /// Collects child output. Invoked from the launcher's reader callbacks, so
    /// it must not block: it appends under a short lock and returns.
    /// </summary>
    private sealed class OutputCapture(ISetupLog log)
    {
        private readonly Lock _gate = new();
        private readonly StringBuilder _output = new();
        private readonly Queue<string> _errorTail = new();
        private bool _outputTruncated;

        public void Append(ProcessOutputLine line)
        {
            lock (_gate)
            {
                if (line.Stream == ProcessOutputStream.StandardError)
                {
                    _errorTail.Enqueue(line.Text);
                    while (_errorTail.Count > StandardErrorTailLines)
                    {
                        _errorTail.Dequeue();
                    }
                }
                else if (_output.Length + line.Text.Length + 1 <= StandardOutputCharacterLimit)
                {
                    _output.Append(line.Text).Append('\n');
                }
                else
                {
                    _outputTruncated = true;
                }
            }

            log.Write((line.Stream == ProcessOutputStream.StandardError ? "  err| " : "  out| ") + line.Text);
        }

        public string Output()
        {
            lock (_gate)
            {
                return _outputTruncated
                    ? _output.ToString() + "\n[output truncated by the gateway]"
                    : _output.ToString();
            }
        }

        public IReadOnlyList<string> ErrorTail()
        {
            lock (_gate)
            {
                return [.. _errorTail];
            }
        }
    }
}
