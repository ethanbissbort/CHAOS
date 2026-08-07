using System.Collections.ObjectModel;

namespace Chaos.Host.Supervisor.Processes;

/// <summary>Which pipe a captured line came from.</summary>
public enum ProcessOutputStream
{
    StandardOutput = 0,
    StandardError = 1,
}

/// <summary>One line of child output, as captured.</summary>
public readonly record struct ProcessOutputLine(ProcessOutputStream Stream, string Text);

/// <summary>Everything needed to launch a child process.</summary>
public sealed record ProcessStartSpec
{
    public required string FileName { get; init; }

    /// <summary>Passed through <c>ProcessStartInfo.ArgumentList</c>: no shell, no quoting bugs.</summary>
    public required IReadOnlyList<string> Arguments { get; init; }

    public required string WorkingDirectory { get; init; }

    /// <summary>Overlaid on the supervisor's environment. A null value removes a variable.</summary>
    public IReadOnlyDictionary<string, string?> Environment { get; init; } =
        ReadOnlyDictionary<string, string?>.Empty;

    /// <summary>
    /// Called for every line the child writes. Invoked from the reader
    /// callbacks, so implementations must not block: the supervisor's handler
    /// only logs and appends to a bounded ring buffer.
    /// </summary>
    public Action<ProcessOutputLine>? OnOutput { get; init; }

    /// <summary>The command line as an operator would type it, for logs.</summary>
    public string ToCommandLine() =>
        string.Join(' ', new[] { FileName }.Concat(Arguments).Select(Quote));

    private static string Quote(string value) =>
        value.Contains(' ', StringComparison.Ordinal) ? "\"" + value + "\"" : value;
}

/// <summary>A launched child, seen through the narrowest useful hole.</summary>
public interface ISupervisedProcess : IDisposable
{
    int Id { get; }

    bool HasExited { get; }

    /// <summary>Exit code once exited; null before that.</summary>
    int? ExitCode { get; }

    /// <summary>
    /// Completes with the exit code once the process has exited and its output
    /// pipes have drained.
    /// </summary>
    Task<int> WaitForExitAsync(CancellationToken cancellationToken);

    /// <summary>
    /// Ask the child to shut down cleanly (SIGTERM, or a console CTRL_BREAK on
    /// Windows). Returns false when there is no channel to ask through —
    /// which the caller must report rather than silently escalate.
    /// </summary>
    bool TryRequestGracefulShutdown(out string detail);

    /// <summary>Kill the child and every descendant. <paramref name="detail"/> says how.</summary>
    void Kill(out string detail);
}

/// <summary>Launches child processes. Faked in tests.</summary>
public interface IProcessLauncher
{
    ISupervisedProcess Start(ProcessStartSpec spec);
}
