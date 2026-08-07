using System.Diagnostics;
using Chaos.Host.Supervisor.Events;
using Chaos.Host.Supervisor.Health;
using Chaos.Host.Supervisor.Runtime;
using Microsoft.Extensions.Logging;

namespace Chaos.Host.Supervisor.Tests.Fakes;

/// <summary>A health endpoint the test turns on and off.</summary>
internal sealed class FakeHealthProbe : IBackendHealthProbe
{
    private readonly Lock _gate = new();
    private bool _healthy;
    private string _detail = "fake: not answering";
    private int _calls;

    public int Calls
    {
        get
        {
            lock (_gate)
            {
                return _calls;
            }
        }
    }

    public void SetHealthy(string detail = "fake: reported ok")
    {
        lock (_gate)
        {
            _healthy = true;
            _detail = detail;
        }
    }

    public void SetUnhealthy(string detail = "fake: connection refused")
    {
        lock (_gate)
        {
            _healthy = false;
            _detail = detail;
        }
    }

    public Task<BackendHealthResult> CheckAsync(Uri endpoint, TimeSpan timeout, CancellationToken cancellationToken)
    {
        lock (_gate)
        {
            _calls++;
            return Task.FromResult(new BackendHealthResult(_healthy, _detail, "0.4.0", "primary"));
        }
    }
}

/// <summary>A runtime resolver with a fixed answer.</summary>
internal sealed class FakeRuntimeResolver : IPythonRuntimeResolver
{
    private BackendRuntimeResolution _resolution = BackendRuntimeResolution.Success(
        new BackendRuntimeDescriptor
        {
            Layout = BackendRuntimeLayout.Development,
            Executable = "/usr/bin/python3",
            BaseArguments = [],
            WorkingDirectory = "/repo",
            Environment = new Dictionary<string, string?>(StringComparer.Ordinal) { ["PYTHONPATH"] = "/repo/src" },
            Description = "DEVELOPMENT layout: interpreter '/usr/bin/python3', PYTHONPATH '/repo/src'",
        },
        ["fake"]);

    public BackendRuntimeResolution Resolve(BackendSupervisorOptions options) => _resolution;

    public void Fail(string detail) => _resolution = BackendRuntimeResolution.Failure(detail, ["fake"]);
}

/// <summary>Records lifecycle events so tests can assert what an operator sees.</summary>
internal sealed class RecordingEventSink : IChaosEventSink
{
    private readonly Lock _gate = new();
    private readonly List<(ChaosEventId Id, ChaosEventLevel Level, string Message)> _events = [];

    public string Description => "recording sink (test)";

    public IReadOnlyList<(ChaosEventId Id, ChaosEventLevel Level, string Message)> Events
    {
        get
        {
            lock (_gate)
            {
                return [.. _events];
            }
        }
    }

    public void Write(ChaosEventId id, ChaosEventLevel level, string message)
    {
        lock (_gate)
        {
            _events.Add((id, level, message));
        }
    }

    public bool Contains(ChaosEventId id) => Events.Any(entry => entry.Id == id);
}

/// <summary>An in-memory filesystem: paths that exist, and nothing else.</summary>
internal sealed class FakeFileSystem : IFileSystem
{
    private readonly HashSet<string> _files = new(StringComparer.Ordinal);
    private readonly HashSet<string> _directories = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _onPath = new(StringComparer.Ordinal);

    public FakeFileSystem AddFile(string path)
    {
        _files.Add(Normalise(path));
        var directory = Path.GetDirectoryName(Normalise(path));
        while (!string.IsNullOrEmpty(directory))
        {
            _directories.Add(directory);
            directory = Path.GetDirectoryName(directory);
        }

        return this;
    }

    public FakeFileSystem AddDirectory(string path)
    {
        _directories.Add(Normalise(path));
        return this;
    }

    public FakeFileSystem AddOnPath(string name, string resolvedPath)
    {
        _onPath[name] = resolvedPath;
        return this;
    }

    public bool FileExists(string path) => _files.Contains(Normalise(path));

    public bool DirectoryExists(string path) => _directories.Contains(Normalise(path));

    public string? FindOnPath(string fileName) => _onPath.GetValueOrDefault(fileName);

    private static string Normalise(string path) =>
        path.Replace('\\', Path.DirectorySeparatorChar)
            .Replace('/', Path.DirectorySeparatorChar)
            .TrimEnd(Path.DirectorySeparatorChar);
}

/// <summary>Captures log records so a test can prove output was really captured.</summary>
internal sealed class CapturingLoggerProvider : ILoggerProvider
{
    private readonly Lock _gate = new();
    private readonly List<string> _lines = [];

    public IReadOnlyList<string> Lines
    {
        get
        {
            lock (_gate)
            {
                return [.. _lines];
            }
        }
    }

    public ILogger CreateLogger(string categoryName) => new CapturingLogger(this, categoryName);

    public void Dispose()
    {
        // Nothing unmanaged.
    }

    private void Add(string line)
    {
        lock (_gate)
        {
            _lines.Add(line);
        }
    }

    private sealed class CapturingLogger(CapturingLoggerProvider owner, string category) : ILogger
    {
        public IDisposable? BeginScope<TState>(TState state)
            where TState : notnull => null;

        public bool IsEnabled(LogLevel logLevel) => true;

        public void Log<TState>(
            LogLevel logLevel,
            EventId eventId,
            TState state,
            Exception? exception,
            Func<TState, Exception?, string> formatter)
        {
            ArgumentNullException.ThrowIfNull(formatter);
            owner.Add($"{logLevel} {category}: {formatter(state, exception)}");
        }
    }
}

/// <summary>
/// Waits for an asynchronous state machine to settle.
/// </summary>
/// <remarks>
/// This polls real time, but it never waits out a backoff: every delay the
/// supervisor takes is virtual (<see cref="FakeClock"/>). All this does is give
/// the loop's continuations a chance to run, with a hard ceiling so a broken
/// test fails in seconds instead of hanging a build.
/// </remarks>
internal static class Eventually
{
    public static async Task IsTrueAsync(
        Func<bool> condition,
        string because,
        TimeSpan? timeout = null)
    {
        var limit = timeout ?? TimeSpan.FromSeconds(5);
        var stopwatch = Stopwatch.StartNew();

        while (stopwatch.Elapsed < limit)
        {
            if (condition())
            {
                return;
            }

            await Task.Delay(2).ConfigureAwait(false);
        }

        throw new TimeoutException(
            $"Condition was still false after {limit.TotalSeconds:0.#}s: {because}");
    }
}
