using Chaos.Host.Configuration;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Setup;

/// <summary>
/// Kicks first-run setup off when the gateway starts, so that launching the
/// desktop shell is the whole procedure.
/// </summary>
/// <remarks>
/// <para>
/// Registered before the backend supervisor, so on a fresh machine the database
/// is being created while the backend is being started rather than afterwards.
/// </para>
/// <para>
/// <b>It does not block startup.</b> Setting a homestead up takes tens of
/// seconds; holding the host's start sequence for that would mean the gateway
/// is not listening, the shell cannot poll <c>/host/setup</c>, and the operator
/// watches a blank window with no way to tell a slow import from a hung one.
/// The run is started here and reported on <c>/host/setup</c> and
/// <c>/health</c>. Until it finishes, <c>/health</c> says so.
/// </para>
/// </remarks>
internal sealed partial class SetupHostedService : IHostedService
{
    private readonly PlatformSetupCoordinator _coordinator;
    private readonly ChaosHostOptions _options;
    private readonly ILogger<SetupHostedService> _logger;

    public SetupHostedService(
        PlatformSetupCoordinator coordinator,
        IOptions<ChaosHostOptions> options,
        ILogger<SetupHostedService> logger)
    {
        ArgumentNullException.ThrowIfNull(options);

        _coordinator = coordinator;
        _options = options.Value;
        _logger = logger;
    }

    /// <inheritdoc/>
    public Task StartAsync(CancellationToken cancellationToken)
    {
        if (!_options.AutoSetup)
        {
            LogDisabled();
            return Task.CompletedTask;
        }

        var acceptance = _coordinator.RequestRun("startup", force: false);
        if (!acceptance.Accepted)
        {
            LogNotStarted(acceptance.Reason, acceptance.Detail);
        }

        return Task.CompletedTask;
    }

    /// <inheritdoc/>
    public Task StopAsync(CancellationToken cancellationToken)
    {
        _coordinator.Shutdown();
        return Task.CompletedTask;
    }

    [LoggerMessage(EventId = 3301, Level = LogLevel.Information,
        Message = "Automatic first-run setup is turned off on this node (Chaos:AutoSetup=false). The gateway will "
                + "neither check nor change the platform database; GET /host/setup reports 'not_started' and "
                + "POST /host/setup/run?force=true still works.")]
    private partial void LogDisabled();

    [LoggerMessage(EventId = 3302, Level = LogLevel.Information,
        Message = "First-run setup was not started at startup ({Reason}): {Detail}")]
    private partial void LogNotStarted(string reason, string detail);
}
