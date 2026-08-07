using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Chaos.Host.Routing;

/// <summary>
/// Runs <see cref="RouteOwnershipValidator"/> during host startup and refuses to
/// start the host if the manifest claims a prefix for .NET that nothing serves.
/// </summary>
/// <remarks>
/// Registered as the <b>first</b> hosted service so it runs before the backend
/// supervisor and the health poller: there is no point starting a Python child
/// process for a gateway that is about to fail.
/// </remarks>
internal sealed class RouteOwnershipStartupCheck : IHostedService
{
    private readonly RouteOwnershipTable _table;
    private readonly NativeRouteInventory _inventory;
    private readonly ILogger<RouteOwnershipStartupCheck> _logger;

    public RouteOwnershipStartupCheck(
        RouteOwnershipTable table,
        NativeRouteInventory inventory,
        ILogger<RouteOwnershipStartupCheck> logger)
    {
        _table = table;
        _inventory = inventory;
        _logger = logger;
    }

    public Task StartAsync(CancellationToken cancellationToken)
    {
        if (!_inventory.Captured)
        {
            throw new RouteOwnershipException(
                "The native route inventory was never captured, so route ownership cannot be validated. "
              + "UseChaosHost() must be called on the WebApplication before the host is started.");
        }

        // Throws RouteOwnershipException, which fails host startup. Deliberate.
        RouteOwnershipValidator.Validate(_table, _inventory.Patterns);

        _logger.LogInformation(
            "Route ownership validated: {Total} prefixes ({Python} proxied to Python, {Dotnet} served natively).\n{Table}",
            _table.Entries.Count,
            _table.PythonCount,
            _table.DotnetCount,
            _table.ToDisplayString().TrimEnd());

        return Task.CompletedTask;
    }

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;
}
