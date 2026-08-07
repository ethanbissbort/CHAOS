namespace Chaos.Host.Abstractions;

/// <summary>
/// The default <see cref="IBackendSupervisor"/>: it supervises nothing and says so.
/// </summary>
/// <remarks>
/// <para>
/// Registered by <c>AddChaosHost()</c> with <c>TryAddSingleton</c> so the
/// gateway runs standalone — during development, in tests, and on a node where
/// the Python backend is started by something else (systemd, a container, a
/// developer's terminal). Proxying still works; only process supervision is absent.
/// </para>
/// <para>
/// It reports <see cref="BackendState.Unknown"/> rather than
/// <see cref="BackendState.Running"/> or <see cref="BackendState.Stopped"/>,
/// because it genuinely does not know. Backend reachability is measured
/// separately by polling, and that measurement is what <c>/health</c> reports
/// as <c>backend</c>.
/// </para>
/// </remarks>
public sealed class NullBackendSupervisor : IBackendSupervisor
{
    /// <inheritdoc/>
    public BackendStatus Status { get; } = new()
    {
        State = BackendState.Unknown,
        Detail = "No backend supervisor is registered; this host does not manage the Python backend process.",
    };

    /// <inheritdoc/>
    public Task StartAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    /// <inheritdoc/>
    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;
}
