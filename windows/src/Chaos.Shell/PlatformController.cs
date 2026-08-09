using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.Versioning;
using System.Security.Principal;
using System.ServiceProcess;
using Chaos.Shell.Core;
using Microsoft.Win32;

namespace Chaos.Shell;

/// <summary>
/// Everything the shell does to Windows to start, stop and watch the platform.
///
/// This is the thin native adapter: it gathers facts, performs one operation at
/// a time, and hands the result straight to Chaos.Shell.Core to be interpreted.
/// Nothing here decides what an operator is told — every sentence comes from
/// ServiceStateInterpreter, HostExecutableLocator or the launcher state machine,
/// all of which are tested off Windows.
///
/// No CLI. No PowerShell. No sc.exe. Service control goes through
/// ServiceController and the platform is launched as a normal child process.
/// </summary>
[SupportedOSPlatform("windows")]
internal sealed class PlatformController : IDisposable
{
    /// <summary>
    /// How long a service is given to reach the state that was asked for. Long
    /// enough for a cold start of the gateway plus its Python child on a
    /// low-power node; short enough that an operator is not left staring.
    /// </summary>
    public static readonly TimeSpan ServiceTimeout = TimeSpan.FromSeconds(60);

    private readonly object _gate = new();

    private Process? _child;
    private WindowsJobObject? _job;
    private bool _disposed;

    public PlatformController(PlatformLogBuffer log)
    {
        ArgumentNullException.ThrowIfNull(log);
        Log = log;
    }

    /// <summary>Captured output of a platform this shell started.</summary>
    public PlatformLogBuffer Log { get; }

    /// <summary>Raised when the managed child exits on its own.</summary>
    public event EventHandler<string>? ChildExited;

    // ------------------------------------------------------------ facts --

    /// <summary>Whether this shell has administrator rights.</summary>
    public static bool IsElevated()
    {
        try
        {
            using var identity = WindowsIdentity.GetCurrent();
            return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
        }
        catch (Exception ex) when (ex is UnauthorizedAccessException or System.Security.SecurityException)
        {
            // Not being able to ask is not the same as being elevated, and the
            // safe reading is the one that keeps offering the operator a way
            // through rather than assuming rights we cannot demonstrate.
            return false;
        }
    }

    /// <summary>Asks the service control manager about the platform service.</summary>
    public static ServiceProbe ProbeService(string serviceName)
    {
        if (string.IsNullOrWhiteSpace(serviceName))
        {
            return new ServiceProbe
            {
                ServiceName = serviceName ?? string.Empty,
                State = PlatformServiceState.NotInstalled,
            };
        }

        try
        {
            using var controller = new ServiceController(serviceName);

            // Touching Status is what actually contacts the service control
            // manager, so this is where "no such service" surfaces.
            var status = (int)controller.Status;

            return new ServiceProbe
            {
                ServiceName = serviceName,
                State = ServiceStateInterpreter.FromServiceControllerStatus(status),
                DisplayName = SafeDisplayName(controller),
            };
        }
        catch (InvalidOperationException ex) when (Win32CodeOf(ex) == 1060)
        {
            return new ServiceProbe
            {
                ServiceName = serviceName,
                State = PlatformServiceState.NotInstalled,
            };
        }
        catch (Exception ex) when (ex is InvalidOperationException or Win32Exception or ArgumentException)
        {
            return new ServiceProbe
            {
                ServiceName = serviceName,
                State = PlatformServiceState.QueryFailed,
                Problem = Innermost(ex),
            };
        }
    }

    /// <summary>Looks for the gateway executable this shell could start.</summary>
    public static HostExecutableProbe LocateExecutable(string? configuredPath)
    {
        var shellDirectory = AppContext.BaseDirectory;

        try
        {
            return HostExecutableLocator.Locate(configuredPath, shellDirectory, File.Exists);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return new HostExecutableProbe
            {
                Searched = true,
                Path = null,
                Source = HostExecutableSource.None,
                Problem = $"The shell could not search for {HostExecutableLocator.FileName}: {ex.Message}",
            };
        }
    }

    /// <summary>Whether the configured gateway address names this machine.</summary>
    public static bool AddressIsThisMachine(string? address)
    {
        string[] local;
        try
        {
            local = System.Net.Dns.GetHostAddresses(Environment.MachineName)
                .Select(a => a.ToString())
                .ToArray();
        }
        catch (Exception ex) when (ex is System.Net.Sockets.SocketException or ArgumentException)
        {
            // Without the list the rule only gets more conservative: loopback
            // and the machine name still match, an unrecognised address does not.
            local = Array.Empty<string>();
        }

        return LocalAddress.IsThisMachine(address, Environment.MachineName, local);
    }

    // -------------------------------------------------------- service --

    /// <summary>
    /// Starts the Windows service and waits for it to reach Running.
    /// </summary>
    /// <remarks>
    /// The wait is a poll rather than <c>WaitForStatus</c> so that the timeout
    /// is ours, the cancellation is ours, and the final state is observed rather
    /// than inferred from which exception came back.
    /// </remarks>
    public Task<ServiceControlOutcome> StartServiceAsync(
        string serviceName, CancellationToken cancellationToken) =>
        ControlAsync(serviceName, start: true, cancellationToken);

    /// <summary>Stops the Windows service and waits for it to reach Stopped.</summary>
    public Task<ServiceControlOutcome> StopServiceAsync(
        string serviceName, CancellationToken cancellationToken) =>
        ControlAsync(serviceName, start: false, cancellationToken);

    private async Task<ServiceControlOutcome> ControlAsync(
        string serviceName, bool start, CancellationToken cancellationToken)
    {
        var verb = start ? "start" : "stop";
        var wanted = start ? ServiceControllerStatus.Running : ServiceControllerStatus.Stopped;
        var started = Stopwatch.StartNew();

        Log.Add(LogStream.Shell, $"Asking Windows to {verb} the service '{serviceName}'.");

        ServiceController controller;
        try
        {
            controller = new ServiceController(serviceName);
        }
        catch (ArgumentException ex)
        {
            return Interpret(start, PlatformServiceState.NotInstalled, started.Elapsed,
                null, ex.Message, serviceName);
        }

        using (controller)
        {
            try
            {
                var before = controller.Status;
                if (before == wanted)
                {
                    return Interpret(start, ServiceStateInterpreter.FromServiceControllerStatus((int)before),
                        TimeSpan.Zero, null, null, serviceName);
                }

                // The call itself blocks on the service control manager, so it
                // goes to the pool rather than to the UI thread.
                await Task.Run(
                    () =>
                    {
                        if (start)
                        {
                            controller.Start();
                        }
                        else
                        {
                            controller.Stop();
                        }
                    },
                    cancellationToken).ConfigureAwait(true);
            }
            catch (Exception ex) when (ex is InvalidOperationException or Win32Exception)
            {
                var outcome = Interpret(start, CurrentState(controller), started.Elapsed,
                    Win32CodeOf(ex), Innermost(ex), serviceName);
                Log.Add(LogStream.Shell, outcome.Message);
                return outcome;
            }

            var final = await PollUntilAsync(controller, wanted, started, cancellationToken)
                .ConfigureAwait(true);

            var result = Interpret(start, final, started.Elapsed, null, null, serviceName);
            Log.Add(LogStream.Shell, result.Message);
            return result;
        }
    }

    private static async Task<PlatformServiceState> PollUntilAsync(
        ServiceController controller,
        ServiceControllerStatus wanted,
        Stopwatch elapsed,
        CancellationToken cancellationToken)
    {
        while (elapsed.Elapsed < ServiceTimeout)
        {
            try
            {
                controller.Refresh();
                if (controller.Status == wanted)
                {
                    return ServiceStateInterpreter.FromServiceControllerStatus((int)wanted);
                }
            }
            catch (Exception ex) when (ex is InvalidOperationException or Win32Exception)
            {
                return PlatformServiceState.QueryFailed;
            }

            try
            {
                await Task.Delay(TimeSpan.FromMilliseconds(400), cancellationToken).ConfigureAwait(true);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }

        return CurrentState(controller);
    }

    private static ServiceControlOutcome Interpret(
        bool start,
        PlatformServiceState finalState,
        TimeSpan waited,
        int? win32Error,
        string? errorMessage,
        string serviceName) =>
        start
            ? ServiceStateInterpreter.InterpretStart(
                finalState, waited, ServiceTimeout, win32Error, errorMessage, serviceName)
            : ServiceStateInterpreter.InterpretStop(
                finalState, waited, ServiceTimeout, win32Error, errorMessage, serviceName);

    private static PlatformServiceState CurrentState(ServiceController controller)
    {
        try
        {
            controller.Refresh();
            return ServiceStateInterpreter.FromServiceControllerStatus((int)controller.Status);
        }
        catch (Exception ex) when (ex is InvalidOperationException or Win32Exception)
        {
            return PlatformServiceState.QueryFailed;
        }
    }

    private static string? SafeDisplayName(ServiceController controller)
    {
        try
        {
            return controller.DisplayName;
        }
        catch (Exception ex) when (ex is InvalidOperationException or Win32Exception)
        {
            return null;
        }
    }

    // ---------------------------------------------------- managed child --

    /// <summary>True while a platform started by this shell is alive.</summary>
    public bool ManagedChildRunning
    {
        get
        {
            lock (_gate)
            {
                return _child is { HasExited: false };
            }
        }
    }

    /// <summary>The child's process id, for the diagnostic.</summary>
    public int? ManagedChildProcessId
    {
        get
        {
            lock (_gate)
            {
                try
                {
                    return _child is { HasExited: false } ? _child.Id : null;
                }
                catch (InvalidOperationException)
                {
                    return null;
                }
            }
        }
    }

    /// <summary>
    /// Starts the platform as a child of this shell.
    /// </summary>
    /// <returns>The failure reason, or null when it started.</returns>
    public string? StartManagedChild(PlatformStartPlan plan)
    {
        ArgumentNullException.ThrowIfNull(plan);

        if (plan.Method != StartMethod.ManagedChild || plan.ExecutablePath is null)
        {
            return plan.Refusal ?? "This shell was not asked to start the platform itself.";
        }

        lock (_gate)
        {
            if (_child is { HasExited: false })
            {
                return "This shell has already started the platform.";
            }

            var info = new ProcessStartInfo(plan.ExecutablePath)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                WorkingDirectory = plan.WorkingDirectory ?? string.Empty,
            };

            foreach (var (name, value) in plan.Environment)
            {
                info.Environment[name] = value;
            }

            var process = new Process { StartInfo = info, EnableRaisingEvents = true };
            process.OutputDataReceived += (_, e) => Log.Add(LogStream.Output, e.Data);
            process.ErrorDataReceived += (_, e) => Log.Add(LogStream.Error, e.Data);
            process.Exited += OnChildExited;

            try
            {
                // The job object is created first so the child can be put in it
                // immediately: a gateway orphaned by a shell crash would keep
                // the port bound and make the next start fail.
                // Not "_job ??= TryCreate(..., out var detail)": with ??= the
                // right side runs only when _job is null, so detail would not be
                // definitely assigned on the reuse path.
                string jobDetail;
                if (_job is null)
                {
                    _job = WindowsJobObject.TryCreate(
                        $"ChaosShell_{Environment.ProcessId}", out jobDetail);
                }
                else
                {
                    jobDetail = "Reusing the job object created for an earlier start.";
                }

                Log.Add(LogStream.Shell, _job is null
                    ? $"Running without a job object ({jobDetail}); a crash of this shell could leave "
                      + "the platform running."
                    : jobDetail);

                Log.Add(LogStream.Shell, $"Starting {plan.ExecutablePath}");
                foreach (var (name, value) in plan.Environment)
                {
                    Log.Add(LogStream.Shell, $"  {name}={value}");
                }

                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();

                if (_job is not null && !_job.TryAssign(process, out var assignDetail))
                {
                    Log.Add(LogStream.Shell, assignDetail);
                }

                _child = process;
                Log.Add(LogStream.Shell,
                    $"Started as process {process.Id}. THIS PLATFORM WILL STOP WHEN THIS SHELL EXITS.");
                return null;
            }
            catch (Exception ex) when (ex is Win32Exception or InvalidOperationException or IOException)
            {
                process.Exited -= OnChildExited;
                process.Dispose();

                var reason = $"The shell could not start {plan.ExecutablePath}: {Innermost(ex)}";
                Log.Add(LogStream.Shell, reason);
                return reason;
            }
        }
    }

    private void OnChildExited(object? sender, EventArgs e)
    {
        if (sender is not Process process)
        {
            return;
        }

        int code;
        try
        {
            code = process.ExitCode;
        }
        catch (InvalidOperationException)
        {
            code = -1;
        }

        var message =
            $"The platform this shell started has EXITED with code {code}. It is no longer "
            + "controlling anything. Read the log below, then start it again.";

        Log.Add(LogStream.Shell, message);
        ChildExited?.Invoke(this, message);
    }

    /// <summary>
    /// Stops a platform this shell started, and everything it spawned.
    /// </summary>
    /// <returns>What happened, for the operator.</returns>
    public string StopManagedChild()
    {
        lock (_gate)
        {
            if (_child is null)
            {
                return "This shell has not started a platform, so there is nothing for it to stop.";
            }

            var process = _child;
            _child = null;
            process.Exited -= OnChildExited;

            try
            {
                if (!process.HasExited)
                {
                    // Kill the tree, then close the job. The gateway supervises
                    // a Python child of its own, and leaving that behind would
                    // hold the loopback port against the next start.
                    process.Kill(entireProcessTree: true);
                    process.WaitForExit(10_000);
                }
            }
            catch (Exception ex) when (ex is Win32Exception or InvalidOperationException or NotSupportedException)
            {
                Log.Add(LogStream.Shell, $"Stopping the platform did not go cleanly: {Innermost(ex)}");
            }
            finally
            {
                process.Dispose();
            }

            if (_job is not null)
            {
                _job.TryTerminate(out var detail);
                Log.Add(LogStream.Shell, detail);
                _job.Dispose();
                _job = null;
            }

            const string Stopped =
                "The platform started by this shell has been STOPPED. Nothing is being controlled "
                + "and no alarms are being evaluated on this node until it is started again.";

            Log.Add(LogStream.Shell, Stopped);
            return Stopped;
        }
    }

    // ------------------------------------------------------- elevation --

    /// <summary>
    /// Restarts this shell with administrator rights, keeping the arguments it
    /// was launched with.
    /// </summary>
    /// <returns>The failure reason, or null when the elevated copy started.</returns>
    public static string? TryRelaunchElevated(IReadOnlyList<string> arguments)
    {
        var executable = Environment.ProcessPath;
        if (string.IsNullOrEmpty(executable))
        {
            return "The shell could not work out its own location, so it cannot restart itself.";
        }

        var info = new ProcessStartInfo(executable)
        {
            UseShellExecute = true,
            Verb = "runas",
        };

        foreach (var argument in arguments)
        {
            info.ArgumentList.Add(argument);
        }

        try
        {
            Process.Start(info);
            return null;
        }
        catch (Win32Exception ex) when (ex.NativeErrorCode == 1223)
        {
            // ERROR_CANCELLED: the operator dismissed the prompt. That is an
            // answer, not a fault, and must not be dressed up as an error.
            return "The administrator prompt was dismissed, so the shell is still running without "
                + "administrator rights.";
        }
        catch (Exception ex) when (ex is Win32Exception or InvalidOperationException)
        {
            return $"The shell could not restart itself as administrator: {Innermost(ex)}";
        }
    }

    // ------------------------------------------------- start with windows --

    /// <summary>Reads the per-user Run entry this shell owns.</summary>
    public static string? ReadStartupRegistration()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(StartupRegistration.RunKeyPath);
            return key?.GetValue(StartupRegistration.ValueName) as string;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Security.SecurityException)
        {
            return null;
        }
    }

    /// <summary>
    /// Applies "start this shell when I sign in".
    /// </summary>
    /// <returns>The failure reason, or null on success.</returns>
    public static string? ApplyStartupRegistration(bool desired)
    {
        var executable = Environment.ProcessPath;
        if (string.IsNullOrEmpty(executable))
        {
            return "The shell could not work out its own location, so it cannot register itself to "
                + "start with Windows.";
        }

        var plan = StartupRegistration.Plan(desired, ReadStartupRegistration(), executable);
        if (plan.Action == StartupRegistrationAction.None)
        {
            return null;
        }

        try
        {
            using var key = Registry.CurrentUser.CreateSubKey(StartupRegistration.RunKeyPath, writable: true);
            if (key is null)
            {
                return "Windows would not open the startup key for this user.";
            }

            if (plan.Action == StartupRegistrationAction.Unregister)
            {
                key.DeleteValue(StartupRegistration.ValueName, throwOnMissingValue: false);
            }
            else
            {
                key.SetValue(StartupRegistration.ValueName, plan.Value!, RegistryValueKind.String);
            }

            return null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Security.SecurityException)
        {
            return $"The shell could not change its startup registration: {ex.Message}";
        }
    }

    // ----------------------------------------------------------- paths --

    /// <summary>Where the shell and the platform write their logs.</summary>
    public static string LogDirectory =>
        Environment.ExpandEnvironmentVariables(@"%ProgramData%\Project CHAOS\logs");

    /// <summary>
    /// Opens the log folder, creating it first. Explorer refuses a path that is
    /// not there, and "nothing happened" is the least useful response a button
    /// can give.
    /// </summary>
    public static string? OpenLogFolder()
    {
        try
        {
            Directory.CreateDirectory(LogDirectory);
            Process.Start(new ProcessStartInfo(LogDirectory) { UseShellExecute = true });
            return null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or Win32Exception)
        {
            return $"The shell could not open {LogDirectory}: {ex.Message}";
        }
    }

    /// <summary>Opens a URL in the operator's browser.</summary>
    public static void OpenInBrowser(Uri uri)
    {
        try
        {
            Process.Start(new ProcessStartInfo(uri.ToString()) { UseShellExecute = true });
        }
        catch (Exception ex) when (ex is Win32Exception or InvalidOperationException)
        {
            // Nothing useful to do: the address is on screen and can be typed.
        }
    }

    // ---------------------------------------------------------- helpers --

    private static int? Win32CodeOf(Exception exception)
    {
        var current = exception;
        while (current is not null)
        {
            if (current is Win32Exception win32)
            {
                return win32.NativeErrorCode;
            }

            current = current.InnerException;
        }

        return null;
    }

    private static string Innermost(Exception exception)
    {
        var current = exception;
        while (current.InnerException is not null)
        {
            current = current.InnerException;
        }

        return current.Message.Trim();
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;

        // Exiting the shell stops a platform the shell started. That is not a
        // side effect to be tidied away quietly — it is the fact the whole
        // launcher exists to make visible, and ExitShell warns before we get here.
        if (ManagedChildRunning)
        {
            StopManagedChild();
        }

        lock (_gate)
        {
            _job?.Dispose();
            _job = null;
            _child?.Dispose();
            _child = null;
        }
    }
}
