namespace Chaos.Host.Supervisor.Time;

/// <summary>
/// Time, injected. Backoff windows, readiness deadlines and shutdown timeouts
/// all go through here so the restart policy can be tested in microseconds
/// instead of minutes.
/// </summary>
public interface IClock
{
    DateTimeOffset UtcNow { get; }

    /// <summary>Completes after <paramref name="duration"/>, or cancels.</summary>
    Task Delay(TimeSpan duration, CancellationToken cancellationToken);
}

/// <summary>Real time.</summary>
public sealed class SystemClock : IClock
{
    public static SystemClock Instance { get; } = new();

    public DateTimeOffset UtcNow => DateTimeOffset.UtcNow;

    public Task Delay(TimeSpan duration, CancellationToken cancellationToken) =>
        duration <= TimeSpan.Zero
            ? Task.CompletedTask
            : Task.Delay(duration, cancellationToken);
}
