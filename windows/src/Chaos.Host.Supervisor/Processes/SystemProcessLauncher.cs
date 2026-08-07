using System.Diagnostics;
using System.Globalization;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;

namespace Chaos.Host.Supervisor.Processes;

/// <summary>
/// Launches real child processes: pipes drained asynchronously, and on Windows
/// the whole tree contained in a Job Object.
/// </summary>
public sealed class SystemProcessLauncher : IProcessLauncher
{
    private readonly ILogger _logger;

    public SystemProcessLauncher()
        : this(NullLogger<SystemProcessLauncher>.Instance)
    {
    }

    public SystemProcessLauncher(ILogger<SystemProcessLauncher> logger)
        : this((ILogger)logger)
    {
    }

    private SystemProcessLauncher(ILogger logger) =>
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));

    public ISupervisedProcess Start(ProcessStartSpec spec)
    {
        ArgumentNullException.ThrowIfNull(spec);
        return new SystemSupervisedProcess(spec, _logger);
    }
}

internal sealed class SystemSupervisedProcess : ISupervisedProcess
{
    private readonly Process _process;
    private readonly ILogger _logger;
    private readonly Action<ProcessOutputLine>? _onOutput;
    private readonly Task<int> _exitTask;
    private readonly WindowsJobObject? _job;
    private bool _disposed;

    internal SystemSupervisedProcess(ProcessStartSpec spec, ILogger logger)
    {
        _logger = logger;
        _onOutput = spec.OnOutput;

        var startInfo = new ProcessStartInfo
        {
            FileName = spec.FileName,
            WorkingDirectory = spec.WorkingDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
        };

        foreach (var argument in spec.Arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        foreach (var (key, value) in spec.Environment)
        {
            if (value is null)
            {
                startInfo.Environment.Remove(key);
            }
            else
            {
                startInfo.Environment[key] = value;
            }
        }

        _process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        _process.OutputDataReceived += (_, e) => Emit(ProcessOutputStream.StandardOutput, e.Data);
        _process.ErrorDataReceived += (_, e) => Emit(ProcessOutputStream.StandardError, e.Data);

        if (!_process.Start())
        {
            _process.Dispose();
            throw new InvalidOperationException(
                $"The operating system did not start a new process for '{spec.FileName}'.");
        }

        Id = _process.Id;

        // Contain the tree before it has a chance to grow. Best effort: a
        // machine without job object rights still gets a supervised backend,
        // just with a weaker guarantee about orphans, and the log says so.
        if (OperatingSystem.IsWindows())
        {
            _job = CreateAndAssignJob(_process);
        }

        // Event-driven reads. Never ReadToEnd(): the platform logs continuously,
        // and a blocking read on one pipe while the other fills is the classic
        // way to deadlock a supervisor against its own child.
        _process.BeginOutputReadLine();
        _process.BeginErrorReadLine();

        // The child is not interactive. Give it an immediate EOF rather than
        // leaving it holding a handle on the service's (nonexistent) console.
        try
        {
            _process.StandardInput.Close();
        }
        catch (IOException)
        {
            // Nothing to close if the child already exited.
        }

        _exitTask = Task.Run(async () =>
        {
            // WaitForExitAsync also waits for the redirected pipes to drain, so
            // by the time this completes we have all the output.
            await _process.WaitForExitAsync(CancellationToken.None).ConfigureAwait(false);
            return _process.ExitCode;
        });
    }

    public int Id { get; }

    public bool HasExited
    {
        get
        {
            try
            {
                return _process.HasExited;
            }
            catch (InvalidOperationException)
            {
                return true;
            }
        }
    }

    public int? ExitCode
    {
        get
        {
            try
            {
                return _process.HasExited ? _process.ExitCode : null;
            }
            catch (InvalidOperationException)
            {
                return null;
            }
        }
    }

    public Task<int> WaitForExitAsync(CancellationToken cancellationToken) =>
        _exitTask.WaitAsync(cancellationToken);

    public bool TryRequestGracefulShutdown(out string detail)
    {
        if (HasExited)
        {
            detail = "already exited";
            return true;
        }

        try
        {
            return GracefulSignals.TryRequestStop(_process.Id, out detail);
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            detail = $"could not signal the child: {ex.Message}";
            return false;
        }
    }

    public void Kill(out string detail)
    {
        if (HasExited)
        {
            detail = "already exited";
            return;
        }

        if (_job is not null && OperatingSystem.IsWindows() && _job.TryTerminate(out var jobDetail))
        {
            detail = jobDetail;
            return;
        }

        try
        {
            _process.Kill(entireProcessTree: true);
            detail = "killed the process tree (Process.Kill(entireProcessTree: true))";
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception or NotSupportedException)
        {
            detail = $"kill failed: {ex.GetType().Name}: {ex.Message}";
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;

        // Order matters: closing the job handle is what guarantees no orphan
        // survives us, so it happens even if disposing the Process throws.
        try
        {
            _process.Dispose();
        }
        finally
        {
            // _job is only ever non-null on Windows; the guard is what tells
            // the platform-compatibility analyzer that.
            if (OperatingSystem.IsWindows())
            {
                _job?.Dispose();
            }
        }
    }

    private WindowsJobObject? CreateAndAssignJob(Process process)
    {
        if (!OperatingSystem.IsWindows())
        {
            return null;
        }

        var name = "ChaosBackend_" +
                   Environment.ProcessId.ToString(CultureInfo.InvariantCulture) + "_" +
                   process.Id.ToString(CultureInfo.InvariantCulture);

        var job = WindowsJobObject.TryCreate(name, out var createDetail);
        if (job is null)
        {
            _logger.LogWarning(
                "Backend process tree is NOT contained in a Windows job object ({Detail}). " +
                "Orphaned grandchildren would survive a kill and could keep the loopback port bound.",
                createDetail);
            return null;
        }

        if (!job.TryAssign(process, out var assignDetail))
        {
            _logger.LogWarning(
                "Backend process could not be assigned to its job object ({Detail}). " +
                "Falling back to Process.Kill(entireProcessTree: true) for shutdown.",
                assignDetail);
            job.Dispose();
            return null;
        }

        _logger.LogDebug("Backend process contained: {Create}; {Assign}", createDetail, assignDetail);
        return job;
    }

    private void Emit(ProcessOutputStream stream, string? data)
    {
        if (data is null)
        {
            return;
        }

        try
        {
            _onOutput?.Invoke(new ProcessOutputLine(stream, data));
        }
        catch (Exception ex)
        {
            // A broken log sink must never take down the reader loop; that
            // would stop the pipe draining and eventually block the child.
            _logger.LogWarning(ex, "A backend output handler threw. The line was dropped.");
        }
    }

}
