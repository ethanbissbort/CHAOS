using System.Collections.ObjectModel;

namespace Chaos.Host.Supervisor.Runtime;

/// <summary>Which Python the backend will be launched with.</summary>
public enum BackendRuntimeLayout
{
    /// <summary>Nothing resolved. Never treat this as a working install.</summary>
    Unresolved = 0,

    /// <summary>
    /// The runtime shipped inside the product — <c>&lt;InstallRoot&gt;\python\python.exe</c>.
    /// This is what a customer machine runs.
    /// </summary>
    Embedded = 1,

    /// <summary>
    /// A developer checkout: whatever <c>python3</c>/<c>python</c> is on PATH,
    /// with <c>PYTHONPATH=&lt;repo&gt;/src</c>.
    /// </summary>
    Development = 2,
}

/// <summary>A resolved, launchable Python backend.</summary>
public sealed record BackendRuntimeDescriptor
{
    public required BackendRuntimeLayout Layout { get; init; }

    /// <summary>Absolute path to the interpreter.</summary>
    public required string Executable { get; init; }

    /// <summary>Interpreter arguments before the CLI's own (e.g. <c>-m homestead_twin.cli</c>).</summary>
    public required IReadOnlyList<string> BaseArguments { get; init; }

    public required string WorkingDirectory { get; init; }

    /// <summary>Environment overlaid on the supervisor's own.</summary>
    public required IReadOnlyDictionary<string, string?> Environment { get; init; }

    /// <summary>One line an operator can read at 2am and understand.</summary>
    public required string Description { get; init; }
}

/// <summary>
/// Outcome of runtime resolution, including every location that was tried.
/// A failure carries the whole trail: "no Python" and "your install is missing
/// its interpreter" are different problems and must not read the same.
/// </summary>
public sealed record BackendRuntimeResolution
{
    private BackendRuntimeResolution(
        BackendRuntimeDescriptor? runtime,
        string detail,
        IReadOnlyList<string> attempts)
    {
        Runtime = runtime;
        Detail = detail;
        Attempts = attempts;
    }

    public BackendRuntimeDescriptor? Runtime { get; }

    /// <summary>Why this resolved the way it did. Always populated.</summary>
    public string Detail { get; }

    /// <summary>Every candidate considered, in order, with the verdict on each.</summary>
    public IReadOnlyList<string> Attempts { get; }

    public bool Succeeded => Runtime is not null;

    public static BackendRuntimeResolution Success(
        BackendRuntimeDescriptor runtime,
        IEnumerable<string> attempts) =>
        new(runtime, runtime.Description, new ReadOnlyCollection<string>(attempts.ToList()));

    public static BackendRuntimeResolution Failure(string detail, IEnumerable<string> attempts) =>
        new(null, detail, new ReadOnlyCollection<string>(attempts.ToList()));

    /// <summary>Detail plus the full attempt trail, for logs and Event Log entries.</summary>
    public string ToReport()
    {
        if (Attempts.Count == 0)
        {
            return Detail;
        }

        return Detail + System.Environment.NewLine +
               string.Join(System.Environment.NewLine, Attempts.Select(a => "  - " + a));
    }
}
