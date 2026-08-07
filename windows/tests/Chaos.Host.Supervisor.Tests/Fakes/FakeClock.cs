using Chaos.Host.Supervisor.Time;

namespace Chaos.Host.Supervisor.Tests.Fakes;

/// <summary>
/// Virtual time. Backoff windows, readiness deadlines and shutdown timeouts are
/// all measured against this, so a test that exercises a two-minute cap costs
/// microseconds and never flakes on a loaded build agent.
/// </summary>
internal sealed class FakeClock : IClock
{
    private readonly Lock _gate = new();
    private readonly List<Waiter> _waiters = [];
    private readonly List<TimeSpan> _requested = [];
    private DateTimeOffset _now;

    public FakeClock(DateTimeOffset? start = null) =>
        _now = start ?? new DateTimeOffset(2026, 1, 1, 0, 0, 0, TimeSpan.Zero);

    /// <summary>
    /// When true, every delay completes immediately and time jumps forward by
    /// the requested amount. Used where a test wants the supervision loop to
    /// run its whole restart sequence without stepping through it.
    /// </summary>
    public bool AutoAdvance { get; set; }

    public DateTimeOffset UtcNow
    {
        get
        {
            lock (_gate)
            {
                return _now;
            }
        }
    }

    /// <summary>Every delay ever asked for, in order.</summary>
    public IReadOnlyList<TimeSpan> Requested
    {
        get
        {
            lock (_gate)
            {
                return [.. _requested];
            }
        }
    }

    public int PendingWaiters
    {
        get
        {
            lock (_gate)
            {
                return _waiters.Count;
            }
        }
    }

    public Task Delay(TimeSpan duration, CancellationToken cancellationToken)
    {
        if (cancellationToken.IsCancellationRequested)
        {
            return Task.FromCanceled(cancellationToken);
        }

        if (duration <= TimeSpan.Zero)
        {
            return Task.CompletedTask;
        }

        Waiter waiter;
        lock (_gate)
        {
            _requested.Add(duration);
            if (AutoAdvance)
            {
                _now += duration;
                return Task.CompletedTask;
            }

            waiter = new Waiter(_now + duration, new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously));
            _waiters.Add(waiter);
        }

        if (cancellationToken.CanBeCanceled)
        {
            var registration = cancellationToken.Register(() =>
            {
                lock (_gate)
                {
                    _waiters.Remove(waiter);
                }

                waiter.Source.TrySetCanceled(cancellationToken);
            });

            _ = waiter.Source.Task.ContinueWith(
                _ => registration.Dispose(),
                CancellationToken.None,
                TaskContinuationOptions.ExecuteSynchronously,
                TaskScheduler.Default);
        }

        return waiter.Source.Task;
    }

    /// <summary>Moves time forward and releases everything now due.</summary>
    public void Advance(TimeSpan by)
    {
        List<Waiter> due;
        lock (_gate)
        {
            _now += by;
            due = [.. _waiters.Where(waiter => waiter.Due <= _now)];
            foreach (var waiter in due)
            {
                _waiters.Remove(waiter);
            }
        }

        foreach (var waiter in due)
        {
            waiter.Source.TrySetResult();
        }
    }

    private sealed record Waiter(DateTimeOffset Due, TaskCompletionSource Source);
}
