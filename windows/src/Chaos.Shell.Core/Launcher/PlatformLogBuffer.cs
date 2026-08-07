namespace Chaos.Shell.Core;

/// <summary>Which stream a captured line came from.</summary>
public enum LogStream
{
    /// <summary>The shell's own commentary — what it did and what happened.</summary>
    Shell = 0,

    /// <summary>The child process's standard output.</summary>
    Output = 1,

    /// <summary>The child process's standard error.</summary>
    Error = 2,
}

/// <summary>One captured line.</summary>
public sealed record PlatformLogLine(DateTimeOffset AtUtc, LogStream Stream, string Text)
{
    /// <summary>"14:22:07  err  something went wrong" — fixed width, for a monospaced view.</summary>
    public string Render() =>
        $"{AtUtc.ToLocalTime():HH:mm:ss}  {Prefix}  {Text}";

    private string Prefix => Stream switch
    {
        LogStream.Output => "out",
        LogStream.Error => "err",
        _ => "···",
    };
}

/// <summary>
/// A bounded, thread-safe record of what a platform started by this shell has
/// said.
/// </summary>
/// <remarks>
/// <para>
/// A managed child has no log file of its own that the operator knows about —
/// its console output is the only account of why it failed to start. That
/// output arrives on a background thread from the process's redirected streams,
/// and it must not be able to grow without limit: a platform stuck in a restart
/// loop for a week would otherwise take the shell down with it.
/// </para>
/// <para>
/// Oldest lines are dropped first, and the count of what was dropped is kept, so
/// the log view can say "1,204 earlier lines were dropped" instead of quietly
/// presenting a partial record as a complete one.
/// </para>
/// </remarks>
public sealed class PlatformLogBuffer
{
    /// <summary>Lines kept by default. Roughly a screenful of history at a glance.</summary>
    public const int DefaultCapacity = 2000;

    private readonly object _gate = new();
    private readonly Queue<PlatformLogLine> _lines;
    private readonly int _capacity;
    private long _dropped;

    public PlatformLogBuffer(int capacity = DefaultCapacity)
    {
        if (capacity < 1)
        {
            throw new ArgumentOutOfRangeException(nameof(capacity), capacity, "The buffer must hold at least one line.");
        }

        _capacity = capacity;
        _lines = new Queue<PlatformLogLine>(Math.Min(capacity, 256));
    }

    /// <summary>How many lines have been discarded to stay inside the capacity.</summary>
    public long Dropped
    {
        get
        {
            lock (_gate)
            {
                return _dropped;
            }
        }
    }

    public int Count
    {
        get
        {
            lock (_gate)
            {
                return _lines.Count;
            }
        }
    }

    /// <summary>Records a line. Null and blank lines are ignored, not stored as blanks.</summary>
    public void Add(LogStream stream, string? text, DateTimeOffset? atUtc = null)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return;
        }

        var line = new PlatformLogLine(atUtc ?? DateTimeOffset.UtcNow, stream, text!.TrimEnd());

        lock (_gate)
        {
            _lines.Enqueue(line);
            while (_lines.Count > _capacity)
            {
                _lines.Dequeue();
                _dropped++;
            }
        }
    }

    /// <summary>A snapshot, oldest first. Safe to enumerate while more arrive.</summary>
    public IReadOnlyList<PlatformLogLine> Snapshot()
    {
        lock (_gate)
        {
            return _lines.ToArray();
        }
    }

    /// <summary>
    /// The whole buffer as text, led by a line saying what was dropped when
    /// anything was.
    /// </summary>
    public string Render()
    {
        IReadOnlyList<PlatformLogLine> lines;
        long dropped;

        lock (_gate)
        {
            lines = _lines.ToArray();
            dropped = _dropped;
        }

        var body = string.Join(Environment.NewLine, lines.Select(l => l.Render()));

        return dropped == 0
            ? body
            : $"… {dropped} earlier line{(dropped == 1 ? string.Empty : "s")} dropped to stay inside "
              + $"{_capacity:N0} lines{Environment.NewLine}{body}";
    }

    public void Clear()
    {
        lock (_gate)
        {
            _lines.Clear();
            _dropped = 0;
        }
    }
}
