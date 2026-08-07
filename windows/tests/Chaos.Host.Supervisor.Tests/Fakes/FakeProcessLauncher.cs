using System.Globalization;
using Chaos.Host.Supervisor.Processes;

namespace Chaos.Host.Supervisor.Tests.Fakes;

/// <summary>A process launcher that launches nothing.</summary>
internal sealed class FakeProcessLauncher : IProcessLauncher
{
    private readonly Lock _gate = new();
    private readonly List<FakeProcess> _processes = [];
    private int _nextId = 4200;

    /// <summary>Set to make <see cref="Start"/> throw, as a real launch can.</summary>
    public Exception? ThrowOnStart { get; set; }

    /// <summary>Applied to each process as it is created.</summary>
    public Action<FakeProcess>? Configure { get; set; }

    public IReadOnlyList<FakeProcess> Processes
    {
        get
        {
            lock (_gate)
            {
                return [.. _processes];
            }
        }
    }

    public IReadOnlyList<ProcessStartSpec> Specs
    {
        get
        {
            lock (_gate)
            {
                return [.. _processes.Select(process => process.Spec)];
            }
        }
    }

    public int StartCount
    {
        get
        {
            lock (_gate)
            {
                return _processes.Count;
            }
        }
    }

    public FakeProcess Latest
    {
        get
        {
            lock (_gate)
            {
                return _processes.Count == 0
                    ? throw new InvalidOperationException("No process has been started.")
                    : _processes[^1];
            }
        }
    }

    public ISupervisedProcess Start(ProcessStartSpec spec)
    {
        if (ThrowOnStart is not null)
        {
            throw ThrowOnStart;
        }

        FakeProcess process;
        lock (_gate)
        {
            process = new FakeProcess(_nextId++, spec);
            _processes.Add(process);
        }

        Configure?.Invoke(process);
        return process;
    }
}

/// <summary>A child process the test drives by hand.</summary>
internal sealed class FakeProcess : ISupervisedProcess
{
    private readonly TaskCompletionSource<int> _exit = new(TaskCreationOptions.RunContinuationsAsynchronously);

    internal FakeProcess(int id, ProcessStartSpec spec)
    {
        Id = id;
        Spec = spec;
    }

    public int Id { get; }

    public ProcessStartSpec Spec { get; }

    public bool HasExited { get; private set; }

    public int? ExitCode { get; private set; }

    /// <summary>Whether a graceful stop request is even possible (Windows service mode: no).</summary>
    public bool CanBeAskedToStop { get; set; } = true;

    /// <summary>Whether being asked to stop actually makes it exit.</summary>
    public bool ExitsWhenAsked { get; set; } = true;

    public bool WasAskedToStop { get; private set; }

    public bool WasKilled { get; private set; }

    public bool WasDisposed { get; private set; }

    public Task<int> WaitForExitAsync(CancellationToken cancellationToken) => _exit.Task.WaitAsync(cancellationToken);

    public bool TryRequestGracefulShutdown(out string detail)
    {
        WasAskedToStop = true;

        if (!CanBeAskedToStop)
        {
            detail = "no graceful stop channel (simulating a console-less Windows child)";
            return false;
        }

        detail = "stop requested";
        if (ExitsWhenAsked)
        {
            Exit(0);
        }

        return true;
    }

    public void Kill(out string detail)
    {
        WasKilled = true;
        detail = "killed the process tree (fake)";
        Exit(137);
    }

    public void Dispose() => WasDisposed = true;

    /// <summary>Makes the process exit with <paramref name="code"/>.</summary>
    public void Exit(int code)
    {
        if (HasExited)
        {
            return;
        }

        HasExited = true;
        ExitCode = code;
        _exit.TrySetResult(code);
    }

    /// <summary>Emits a line as the child would have.</summary>
    public void Write(ProcessOutputStream stream, string text) =>
        Spec.OnOutput?.Invoke(new ProcessOutputLine(stream, text));

    public override string ToString() =>
        $"fake pid {Id.ToString(CultureInfo.InvariantCulture)} " +
        (HasExited ? $"(exited {ExitCode?.ToString(CultureInfo.InvariantCulture) ?? "?"})" : "(running)");
}
