namespace Chaos.Host.Abstractions;

/// <summary>
/// Owns the lifecycle of the Python platform backend process.
/// </summary>
/// <remarks>
/// <para>
/// <b>This interface is the seam between <c>Chaos.Host</c> and
/// <c>Chaos.Host.Supervisor</c>.</b> The gateway does not start, stop, restart
/// or monitor the Python process itself; it only calls this interface and
/// reports what it says. The supervisor project implements it.
/// </para>
/// <para><b>Registering an implementation</b></para>
/// <para>
/// The gateway registers <see cref="NullBackendSupervisor"/> with
/// <c>TryAddSingleton</c> during <c>AddChaosHost()</c>. Either order works:
/// </para>
/// <code>
/// // Before AddChaosHost(): TryAdd sees yours and leaves it alone.
/// builder.Services.AddSingleton&lt;IBackendSupervisor, PythonBackendSupervisor&gt;();
/// builder.AddChaosHost();
///
/// // Or after AddChaosHost(): a plain AddSingleton wins the last-one-wins
/// // resolution used by GetRequiredService.
/// builder.AddChaosHost();
/// builder.Services.AddSingleton&lt;IBackendSupervisor, PythonBackendSupervisor&gt;();
/// </code>
/// <para><b>Lifecycle contract the gateway guarantees</b></para>
/// <list type="number">
/// <item><description>
/// <see cref="StartAsync"/> is called once, from a hosted service, during host
/// startup — after route-ownership validation and before the backend health
/// poller starts. The cancellation token is cancelled after
/// <c>Chaos:BackendStartTimeout</c> (default 60s).
/// </description></item>
/// <item><description>
/// <b>A failure in <see cref="StartAsync"/> does not take the gateway down.</b>
/// The exception is logged and surfaced on <c>GET /health</c> and
/// <c>GET /host/info</c>. This is deliberate: a gateway that dies with its
/// backend cannot tell anyone why the platform is unreachable, and reporting
/// the failure is more valuable than exiting.
/// </description></item>
/// <item><description>
/// <see cref="StopAsync"/> is called during graceful shutdown, bounded by
/// <c>Chaos:ShutdownTimeout</c>. In-flight proxied requests are drained first.
/// </description></item>
/// <item><description>
/// <see cref="Status"/> is read on every <c>/health</c> and <c>/host/info</c>
/// request, from arbitrary threads. It must be non-blocking, must not throw,
/// and must be safe for concurrent reads.
/// </description></item>
/// </list>
/// <para><b>What the gateway does *not* do</b></para>
/// <para>
/// It does not restart the backend, does not interpret
/// <see cref="BackendStatus.LastExitCode"/>, and does not treat
/// <see cref="BackendState.Running"/> as proof the backend is serving HTTP —
/// reachability is measured independently by polling
/// <c>Chaos:BackendHealthPath</c> on <c>Chaos:BackendUrl</c>. Restart policy,
/// backoff and crash-loop detection belong to the implementation.
/// </para>
/// </remarks>
public interface IBackendSupervisor
{
    /// <summary>
    /// The current view of the backend. Must not block, must not throw, and
    /// must be safe to read concurrently.
    /// </summary>
    BackendStatus Status { get; }

    /// <summary>
    /// Starts the backend process. Called once during host startup.
    /// </summary>
    /// <param name="cancellationToken">
    /// Cancelled when <c>Chaos:BackendStartTimeout</c> elapses or the host is
    /// shutting down. An implementation that cannot honour it will be waited on
    /// no longer than that budget by the gateway.
    /// </param>
    /// <returns>A task that completes when the backend has been started, or has definitively failed to start.</returns>
    Task StartAsync(CancellationToken cancellationToken);

    /// <summary>
    /// Stops the backend process. Called during graceful shutdown, and safe to
    /// call when the backend was never started.
    /// </summary>
    /// <param name="cancellationToken">Cancelled when the shutdown budget is exhausted.</param>
    /// <returns>A task that completes when the backend has stopped or the budget has been spent.</returns>
    Task StopAsync(CancellationToken cancellationToken);
}
