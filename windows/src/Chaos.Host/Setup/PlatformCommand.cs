namespace Chaos.Host.Setup;

/// <summary>One invocation of the platform's own CLI.</summary>
/// <remarks>
/// Setup never reimplements what the CLI does. <c>init-db</c> and
/// <c>load-all --skip-missing</c> are the tested paths, and
/// <c>status --json</c> is the platform's own account of its database. Copying
/// any of that into C# would create a second source of truth that drifts the
/// first time a table is added.
/// </remarks>
internal sealed record PlatformCommandRequest
{
    /// <summary>
    /// Arguments after <c>-m chaos.cli</c>, for example
    /// <c>["load-all", "--skip-missing"]</c>.
    /// </summary>
    public required IReadOnlyList<string> Arguments { get; init; }

    /// <summary>Why the gateway is running this, in operator English.</summary>
    public required string Purpose { get; init; }

    /// <summary>How long the command may take before it is killed.</summary>
    public required TimeSpan Timeout { get; init; }
}

/// <summary>The result of one platform CLI invocation.</summary>
internal sealed record PlatformCommandResult
{
    /// <summary>How it ended.</summary>
    public required PlatformCommandOutcome Outcome { get; init; }

    /// <summary>The command line as an operator would type it.</summary>
    public required string CommandLine { get; init; }

    /// <summary>Where it ran, or null when nothing was launched.</summary>
    public string? WorkingDirectory { get; init; }

    /// <summary>Exit code, or null when the process never ran or was killed.</summary>
    public int? ExitCode { get; init; }

    /// <summary>Everything the command wrote to stdout.</summary>
    public string StandardOutput { get; init; } = string.Empty;

    /// <summary>The last lines written to stderr, oldest first.</summary>
    public IReadOnlyList<string> StandardErrorTail { get; init; } = [];

    /// <summary>When it started.</summary>
    public required DateTimeOffset StartedUtc { get; init; }

    /// <summary>How long it took.</summary>
    public TimeSpan Duration { get; init; }

    /// <summary>
    /// Why it failed, in the platform's own words where it produced any, or
    /// null on success.
    /// </summary>
    public string? Error { get; init; }

    /// <summary>How the Python runtime resolved, or null when it did not.</summary>
    public string? RuntimeDescription { get; init; }

    /// <summary>Whether the command exited zero.</summary>
    public bool Succeeded => Outcome == PlatformCommandOutcome.Succeeded;

    /// <summary>Turns this result into the record shown on <c>GET /host/setup</c>.</summary>
    /// <param name="purpose">Why the command was run.</param>
    /// <returns>The report.</returns>
    public SetupCommandReport ToReport(string purpose) => new(
        CommandLine,
        purpose,
        StartedUtc,
        Outcome == PlatformCommandOutcome.CouldNotStart ? null : Math.Round(Duration.TotalSeconds, 2),
        ExitCode,
        Outcome,
        Error);
}

/// <summary>
/// Runs the platform's CLI as a child process.
/// </summary>
/// <remarks>
/// The seam the tests replace. The real implementation
/// (<see cref="PlatformCommandRunner"/>) resolves the interpreter and launches
/// the process through <c>Chaos.Host.Supervisor</c>'s own abstractions, so
/// setup and the supervised backend always run against the same Python.
/// </remarks>
internal interface IPlatformCommandRunner
{
    /// <summary>Runs one CLI command to completion.</summary>
    /// <param name="request">What to run.</param>
    /// <param name="cancellationToken">Cancellation — shutdown, or the overall setup budget.</param>
    /// <returns>The result. Never throws for an ordinary failure; failure is a value.</returns>
    Task<PlatformCommandResult> RunAsync(PlatformCommandRequest request, CancellationToken cancellationToken);
}
