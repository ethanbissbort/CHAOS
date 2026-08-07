using Chaos.Host.Routing;

namespace Chaos.Host.Tests;

/// <summary>
/// The guard that stops a migration flip from silently deleting an endpoint.
/// </summary>
public sealed class RouteOwnershipValidatorTests
{
    [Fact]
    public void A_dotnet_route_with_no_endpoint_is_rejected()
    {
        var table = RouteOwnershipTable.Build(
        [
            new RouteOwnership { PathPrefix = "/api/v1", Owner = RouteOwner.Python },
            new RouteOwnership
            {
                PathPrefix = "/api/v1/alarms/definitions",
                Owner = RouteOwner.Dotnet,
                PortedInVersion = "0.6.0",
                Notes = "flipped before the endpoint was written",
            },
        ]);

        var exception = Assert.Throws<RouteOwnershipException>(
            () => RouteOwnershipValidator.Validate(table, ["/health", "/host/info"]));

        Assert.Contains("/api/v1/alarms/definitions", exception.Message, StringComparison.Ordinal);
        Assert.Contains("0.6.0", exception.Message, StringComparison.Ordinal);
        Assert.Contains("flipped before the endpoint was written", exception.Message, StringComparison.Ordinal);

        // The message has to be usable at 3am: it lists what *is* registered.
        Assert.Contains("/host/info", exception.Message, StringComparison.Ordinal);
        Assert.Contains("404", exception.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void A_dotnet_route_backed_by_an_endpoint_at_the_prefix_passes()
    {
        var table = RouteOwnershipTable.Build(
            [new RouteOwnership { PathPrefix = "/health", Owner = RouteOwner.Dotnet }]);

        RouteOwnershipValidator.Validate(table, ["/health"]);
    }

    [Fact]
    public void A_dotnet_route_backed_by_an_endpoint_beneath_the_prefix_passes()
    {
        var table = RouteOwnershipTable.Build(
            [new RouteOwnership { PathPrefix = "/host", Owner = RouteOwner.Dotnet }]);

        RouteOwnershipValidator.Validate(table, ["/host/info", "/host/routes"]);
    }

    [Fact]
    public void A_parameterised_endpoint_backs_its_prefix()
    {
        var table = RouteOwnershipTable.Build(
            [new RouteOwnership { PathPrefix = "/api/v1/alarms/definitions", Owner = RouteOwner.Dotnet }]);

        RouteOwnershipValidator.Validate(table, ["/api/v1/alarms/definitions/{alarmKey}"]);
    }

    [Fact]
    public void A_broader_endpoint_does_not_count_as_backing_a_narrower_dotnet_prefix()
    {
        // /api/v1/alarms/{id} would in practice match /api/v1/alarms/definitions,
        // but relying on that is exactly the kind of accident this check exists to
        // catch. The rule is deliberately strict: the endpoint must live at or
        // beneath the prefix that claims it.
        var table = RouteOwnershipTable.Build(
            [new RouteOwnership { PathPrefix = "/api/v1/alarms/definitions", Owner = RouteOwner.Dotnet }]);

        Assert.Throws<RouteOwnershipException>(
            () => RouteOwnershipValidator.Validate(table, ["/api/v1/alarms/{id}"]));
    }

    [Fact]
    public void Python_routes_need_no_endpoint()
    {
        var table = RouteOwnershipTable.Build(RouteOwnershipDefaults.Manifest);

        RouteOwnershipValidator.Validate(table, ["/health", "/health/live", "/host/info", "/host/routes"]);
    }

    [Fact]
    public void The_shipped_manifest_is_satisfied_by_the_endpoints_this_host_registers()
    {
        // Guards against someone adding a Dotnet row to the compiled-in manifest
        // without the endpoint. The integration tests prove the same thing
        // end to end; this fails faster and points straight at the manifest.
        var table = RouteOwnershipTable.Build(RouteOwnershipDefaults.Manifest);

        RouteOwnershipValidator.Validate(table, ["/", "/ui", "/health", "/health/live", "/host/info", "/host/routes"]);
    }

    [Fact]
    public void Every_unbacked_route_is_reported_not_just_the_first()
    {
        var table = RouteOwnershipTable.Build(
        [
            new RouteOwnership { PathPrefix = "/api/v1/alarms", Owner = RouteOwner.Dotnet },
            new RouteOwnership { PathPrefix = "/api/v1/energy", Owner = RouteOwner.Dotnet },
        ]);

        var exception = Assert.Throws<RouteOwnershipException>(
            () => RouteOwnershipValidator.Validate(table, []));

        Assert.Contains("/api/v1/alarms", exception.Message, StringComparison.Ordinal);
        Assert.Contains("/api/v1/energy", exception.Message, StringComparison.Ordinal);
        Assert.Contains("(none)", exception.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void The_inventory_normalises_patterns_before_they_are_matched()
    {
        var inventory = new NativeRouteInventory();
        inventory.Add("host/routes");
        inventory.Add("/health/");

        Assert.Contains("/host/routes", inventory.Patterns);
        Assert.Contains("/health", inventory.Patterns);
        Assert.Equal(["/health", "/host/routes"], inventory.Patterns);
        Assert.Equal(["/host/routes"], inventory.PatternsUnder("/host"));
    }
}
