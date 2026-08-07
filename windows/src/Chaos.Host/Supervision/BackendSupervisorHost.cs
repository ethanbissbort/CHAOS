using Chaos.Host.Abstractions;
using Chaos.Host.Configuration;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Supervision;

/// <summary>
/// Drives the registered <see cref="IBackendSupervisor"/> through the host
/// lifecycle, without letting it take the gateway down with it.
/// </summary>
/// <remarks>
/// <para>
/// Start is bounded by <see cref="ChaosHostOptions.BackendStartTimeout"/>, and a
/// failure or timeout is logged and reported rather than rethrown. That is the
/// deliberate choice: if the Python backend will not start, the most useful
/// thing this process can do is stay up and say so on <c>/health</c>. A gateway
/// that exits alongside its backend leaves an operator with a refused connection
/// and no explanation, on a site where the next diagnostic step might be a drive
/// to a container in the dark.
/// </para>
/// <para>
/// Stop is bounded by the host's shutdown timeout and is best-effort for the
/// same reason.
/// </para>
/// </remarks>
internal sealed partial class BackendSupervisorHost : IHostedService
{
    private readonly IBackendSupervisor _supervisor;
    private readonly ChaosHostOptions _options;
    private readonly ILogger<BackendSupervisorHost> _logger;

    public BackendSupervisorHost(
        IBackendSupervisor supervisor,
        IOptions<ChaosHostOptions> options,
        ILogger<BackendSupervisorHost> logger)
    {
        _supervisor = supervisor;
        _options = options.Value;
        _logger = logger;
    }

    public async Task StartAsync(CancellationToken cancellationToken)
    {
        if (_supervisor is NullBackendSupervisor)
        {
            LogNoSupervisor(_options.BackendUrl);
            return;
        }

        LogStarting(_supervisor.GetType().FullName ?? _supervisor.GetType().Name, _options.BackendStartTimeout);

        using var budget = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        budget.CancelAfter(_options.BackendStartTimeout);

        try
        {
            await _supervisor.StartAsync(budget.Token).ConfigureAwait(false);
            LogStarted(_supervisor.Status.State.ToString());
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            LogStartTimedOut(_options.BackendStartTimeout);
        }
        catch (Exception ex)
        {
            LogStartFailed(ex);
        }
    }

    public async Task StopAsync(CancellationToken cancellationToken)
    {
        if (_supervisor is NullBackendSupervisor)
        {
            return;
        }

        try
        {
            await _supervisor.StopAsync(cancellationToken).ConfigureAwait(false);
            LogStopped(_supervisor.Status.State.ToString());
        }
        catch (Exception ex)
        {
            LogStopFailed(ex);
        }
    }

    [LoggerMessage(EventId = 3001, Level = LogLevel.Information,
        Message = "No backend supervisor is registered. This host will proxy to {BackendUrl} but does not manage "
                + "the Python backend process; start it separately.")]
    private partial void LogNoSupervisor(string backendUrl);

    [LoggerMessage(EventId = 3002, Level = LogLevel.Information,
        Message = "Starting the platform backend via {Supervisor} (budget {StartTimeout}).")]
    private partial void LogStarting(string supervisor, TimeSpan startTimeout);

    [LoggerMessage(EventId = 3003, Level = LogLevel.Information,
        Message = "Backend supervisor reports state {State} after start.")]
    private partial void LogStarted(string state);

    [LoggerMessage(EventId = 3004, Level = LogLevel.Error,
        Message = "The backend supervisor did not finish starting the platform backend within {StartTimeout}. "
                + "The gateway is staying up so that /health can report this; the platform API will answer 503 "
                + "until the backend is reachable.")]
    private partial void LogStartTimedOut(TimeSpan startTimeout);

    [LoggerMessage(EventId = 3005, Level = LogLevel.Error,
        Message = "The backend supervisor failed to start the platform backend. The gateway is staying up so that "
                + "/health can report this; the platform API will answer 503 until the backend is reachable.")]
    private partial void LogStartFailed(Exception exception);

    [LoggerMessage(EventId = 3006, Level = LogLevel.Information,
        Message = "Backend supervisor reports state {State} after stop.")]
    private partial void LogStopped(string state);

    [LoggerMessage(EventId = 3007, Level = LogLevel.Warning,
        Message = "The backend supervisor failed to stop the platform backend cleanly.")]
    private partial void LogStopFailed(Exception exception);
}
