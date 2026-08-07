using System.Collections.Concurrent;
using System.Text.Json;
using Chaos.Host.Setup;

namespace Chaos.Host.Tests.Support;

/// <summary>
/// Stands in for the platform CLI.
/// </summary>
/// <remarks>
/// Setup's only contact with Python is <see cref="IPlatformCommandRunner"/>, so
/// replacing it here is what makes every setup test hermetic: no interpreter, no
/// database, no filesystem outside a temp directory, and a crash-on-init-db is
/// as easy to stage as a happy path.
/// </remarks>
internal sealed class FakePlatformCommandRunner : IPlatformCommandRunner
{
    private readonly ConcurrentQueue<string> _invocations = new();
    private readonly Func<string, int, PlatformCommandResult> _respond;
    private int _calls;

    /// <param name="respond">
    /// Called with the first CLI argument (<c>status</c>, <c>init-db</c>,
    /// <c>load-all</c>) and the 1-based call number.
    /// </param>
    public FakePlatformCommandRunner(Func<string, int, PlatformCommandResult> respond) => _respond = respond;

    /// <summary>Every command line, in the order it was asked for.</summary>
    public IReadOnlyList<string> Invocations => [.. _invocations];

    /// <summary>The working directory reported back on every result.</summary>
    public string WorkingDirectory { get; set; } = Path.Combine(Path.GetTempPath(), "chaos-tests-workdir");

    /// <summary>
    /// Set to hold the next call until it is completed. Used to prove that a
    /// second run request while one is in flight does not start a second run.
    /// </summary>
    public TaskCompletionSource? Gate { get; set; }

    /// <summary>Signalled once a gated call has actually been entered.</summary>
    public TaskCompletionSource Entered { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

    /// <inheritdoc/>
    public async Task<PlatformCommandResult> RunAsync(
        PlatformCommandRequest request,
        CancellationToken cancellationToken)
    {
        var verb = request.Arguments.Count > 0 ? request.Arguments[0] : string.Empty;
        _invocations.Enqueue(string.Join(' ', request.Arguments));
        var call = Interlocked.Increment(ref _calls);

        Entered.TrySetResult();

        if (Gate is { } gate)
        {
            await gate.Task.WaitAsync(cancellationToken);
        }

        var result = _respond(verb, call);
        return result with
        {
            CommandLine = "python -m chaos.cli " + string.Join(' ', request.Arguments),
            WorkingDirectory = result.WorkingDirectory ?? WorkingDirectory,
            StartedUtc = DateTimeOffset.UtcNow,
            RuntimeDescription = result.RuntimeDescription
                ?? "TEST runtime: no interpreter was launched.",
        };
    }

    /// <summary>A successful command that printed <paramref name="stdout"/>.</summary>
    public static PlatformCommandResult Ok(string stdout = "") => new()
    {
        Outcome = PlatformCommandOutcome.Succeeded,
        CommandLine = "python -m chaos.cli",
        ExitCode = 0,
        StandardOutput = stdout,
        StartedUtc = DateTimeOffset.UtcNow,
    };

    /// <summary>A command that ran and exited non-zero.</summary>
    public static PlatformCommandResult Fails(int exitCode, string error, params string[] stderr) => new()
    {
        Outcome = PlatformCommandOutcome.Failed,
        CommandLine = "python -m chaos.cli",
        ExitCode = exitCode,
        StandardErrorTail = stderr,
        Error = error,
        StartedUtc = DateTimeOffset.UtcNow,
    };

    /// <summary>A command that never launched at all.</summary>
    public static PlatformCommandResult CouldNotStart(string error) => new()
    {
        Outcome = PlatformCommandOutcome.CouldNotStart,
        CommandLine = "python -m chaos.cli",
        Error = error,
        StartedUtc = DateTimeOffset.UtcNow,
    };
}

/// <summary>Builds <c>chaos status --json</c> payloads.</summary>
/// <remarks>
/// The field names mirror <c>_status_payload</c> in the platform CLI. If that
/// payload changes shape, these tests are where it should be noticed.
/// </remarks>
internal static class PlatformStatusJson
{
    /// <summary>Every table the platform declares, as the CLI would list them.</summary>
    public static readonly string[] AllTables =
    [
        "assets", "points", "point_bindings", "power_load_profiles", "alarm_definitions",
        "current_state", "telemetry_samples", "ingest_dead_letters", "commands",
        "work_orders", "commissioning_records",
    ];

    /// <summary>A machine with no schema and no rows.</summary>
    public static string Fresh(string databaseUrl = "sqlite:////var/chaos/homestead.db") =>
        Build(databaseUrl, missingTables: AllTables, counts: new Dictionary<string, int>(StringComparer.Ordinal));

    /// <summary>A machine with a complete schema and the design package loaded.</summary>
    public static string Loaded(string databaseUrl = "sqlite:////var/chaos/homestead.db") =>
        Build(databaseUrl, missingTables: [], counts: new Dictionary<string, int>(StringComparer.Ordinal)
        {
            ["assets"] = 90,
            ["points"] = 701,
            ["point_bindings"] = 245,
            ["power_load_profiles"] = 12,
            ["alarm_definitions"] = 40,
            ["current_state"] = 0,
            ["telemetry_samples"] = 0,
        });

    /// <summary>A payload with whatever the caller wants in it.</summary>
    public static string Build(
        string databaseUrl,
        IReadOnlyList<string> missingTables,
        IReadOnlyDictionary<string, int> counts,
        bool databaseReachable = true)
    {
        var payload = new
        {
            platform_version = "0.4.0",
            node_role = "primary",
            site_id = "site.site.primary.01",
            database = databaseUrl,
            database_reachable = databaseReachable,
            physical_control_enabled = false,
            counts,
            energy_state = (object?)null,
            active_alarms = new { total = 0 },
            missing_tables = missingTables,
            notes = Array.Empty<string>(),
        };

        return JsonSerializer.Serialize(payload);
    }
}
