using Chaos.Host.Routing;
using Microsoft.Extensions.Configuration;

namespace Chaos.Host.Tests;

/// <summary>
/// The matching rules of the migration seam. If these are wrong, requests reach
/// the wrong component and nobody finds out until something does not work.
/// </summary>
public sealed class RouteOwnershipTableTests
{
    [Fact]
    public void Longest_prefix_wins_so_a_subsystem_can_be_ported_piecewise()
    {
        // The scenario the seam exists for: alarm *definitions* have been ported
        // to .NET while the rest of the alarm subsystem is still Python.
        var table = RouteOwnershipTable.Build(
        [
            new RouteOwnership { PathPrefix = "/api/v1", Owner = RouteOwner.Python },
            new RouteOwnership { PathPrefix = "/api/v1/alarms", Owner = RouteOwner.Python },
            new RouteOwnership
            {
                PathPrefix = "/api/v1/alarms/definitions",
                Owner = RouteOwner.Dotnet,
                PortedInVersion = "0.6.0",
            },
        ]);

        Assert.Equal(RouteOwner.Dotnet, table.Match("/api/v1/alarms/definitions")!.Owner);
        Assert.Equal(RouteOwner.Dotnet, table.Match("/api/v1/alarms/definitions/battery_cell_imbalance")!.Owner);

        Assert.Equal(RouteOwner.Python, table.Match("/api/v1/alarms")!.Owner);
        Assert.Equal(RouteOwner.Python, table.Match("/api/v1/alarms/active")!.Owner);
        Assert.Equal(RouteOwner.Python, table.Match("/api/v1/alarms/8f21-abcd")!.Owner);

        Assert.Equal("/api/v1/alarms/definitions", table.Match("/api/v1/alarms/definitions")!.PathPrefix);
        Assert.Equal("/api/v1/alarms", table.Match("/api/v1/alarms/active")!.PathPrefix);
    }

    [Fact]
    public void Matching_respects_segment_boundaries()
    {
        var table = RouteOwnershipTable.Build(
            [new RouteOwnership { PathPrefix = "/api/v1/alarms", Owner = RouteOwner.Python }]);

        Assert.NotNull(table.Match("/api/v1/alarms"));
        Assert.NotNull(table.Match("/api/v1/alarms/"));
        Assert.NotNull(table.Match("/api/v1/alarms/active"));

        // A prefix must not swallow a sibling that merely starts with the same
        // characters: /alarms owning /alarms-archive would be a silent misroute.
        Assert.Null(table.Match("/api/v1/alarmsdefinitions"));
        Assert.Null(table.Match("/api/v1/alarms-archive"));
    }

    [Fact]
    public void Matching_is_case_insensitive_so_a_case_flip_cannot_escape_ownership()
    {
        var table = RouteOwnershipTable.Build(
            [new RouteOwnership { PathPrefix = "/api/v1/commands", Owner = RouteOwner.Python }]);

        Assert.Equal(RouteOwner.Python, table.Match("/API/V1/Commands")!.Owner);
        Assert.Equal(RouteOwner.Python, table.Match("/api/V1/COMMANDS/abc")!.Owner);
    }

    [Fact]
    public void An_unknown_route_is_not_claimed_and_is_therefore_not_proxied()
    {
        var table = RouteOwnershipTable.Build(RouteOwnershipDefaults.Manifest);

        Assert.Null(table.Match("/"));
        Assert.Null(table.Match("/ui/annunciator.html"));
        Assert.Null(table.Match("/metrics"));
        Assert.Null(table.Match("/api/v2/assets"));
        Assert.Null(table.Match(null));
        Assert.Null(table.Match(string.Empty));
    }

    [Fact]
    public void The_shipped_manifest_proxies_every_api_route_to_python_today()
    {
        var table = RouteOwnershipTable.Build(RouteOwnershipDefaults.Manifest);

        var apiRows = table.Entries.Where(entry => entry.PathPrefix.StartsWith("/api/v1", StringComparison.Ordinal)).ToList();

        Assert.NotEmpty(apiRows);
        Assert.All(apiRows, row => Assert.Equal(RouteOwner.Python, row.Owner));
        Assert.All(apiRows, row => Assert.Null(row.PortedInVersion));

        // Every documented subsystem in docs/api.md resolves to a Python owner.
        string[] documentedPaths =
        [
            "/api/v1/overview", "/api/v1/assets/energy.pump.well.01", "/api/v1/points",
            "/api/v1/registry/summary", "/api/v1/telemetry/current", "/api/v1/commands",
            "/api/v1/operating-modes/site/site_01", "/api/v1/audit", "/api/v1/energy/state",
            "/api/v1/alarms/active", "/api/v1/incidents", "/api/v1/notifications",
            "/api/v1/annunciator", "/api/v1/maintenance/plans", "/api/v1/work-orders",
            "/api/v1/commissioning/steps", "/openapi.json", "/docs", "/redoc",
        ];

        foreach (var path in documentedPaths)
        {
            var match = table.Match(path);
            Assert.True(match is not null, $"The manifest does not claim '{path}', so it would not be proxied.");
            Assert.Equal(RouteOwner.Python, match!.Owner);
        }

        // A route the Python app grows tomorrow still proxies via the catch-all.
        Assert.Equal(RouteOwner.Python, table.Match("/api/v1/something-new-next-month")!.Owner);
    }

    [Fact]
    public void The_gateways_own_routes_are_dotnet_owned()
    {
        var table = RouteOwnershipTable.Build(RouteOwnershipDefaults.Manifest);

        Assert.Equal(RouteOwner.Dotnet, table.Match("/health")!.Owner);
        Assert.Equal(RouteOwner.Dotnet, table.Match("/health/live")!.Owner);
        Assert.Equal(RouteOwner.Dotnet, table.Match("/host/info")!.Owner);
        Assert.Equal(RouteOwner.Dotnet, table.Match("/host/routes")!.Owner);
    }

    [Fact]
    public void Configuration_overrides_a_default_row_and_records_that_it_did()
    {
        var table = RouteOwnershipTable.Build(
            RouteOwnershipDefaults.Manifest,
            [
                new RouteOwnership
                {
                    PathPrefix = "/api/v1/alarms/definitions",
                    Owner = RouteOwner.Dotnet,
                    PortedInVersion = "0.6.0",
                },
                new RouteOwnership { PathPrefix = "/api/v1/alarms", Owner = RouteOwner.Python, Notes = "restated" },
            ]);

        var added = table.Entries.Single(entry => entry.PathPrefix == "/api/v1/alarms/definitions");
        Assert.Equal(RouteOwnershipSource.Configuration, added.Source);

        var overridden = table.Entries.Single(entry => entry.PathPrefix == "/api/v1/alarms");
        Assert.Equal(RouteOwnershipSource.ConfigurationOverride, overridden.Source);
        Assert.Equal("restated", overridden.Notes);

        var untouched = table.Entries.Single(entry => entry.PathPrefix == "/api/v1/energy");
        Assert.Equal(RouteOwnershipSource.Default, untouched.Source);
    }

    [Fact]
    public void Replace_defaults_uses_only_the_configured_rows()
    {
        var table = RouteOwnershipTable.Build(
            RouteOwnershipDefaults.Manifest,
            [new RouteOwnership { PathPrefix = "/api/v1", Owner = RouteOwner.Python }],
            replaceDefaults: true);

        Assert.Single(table.Entries);
        Assert.Null(table.Match("/health"));
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("api/v1/alarms")]
    [InlineData("/")]
    [InlineData("/api/v1/alarms?state=active")]
    [InlineData("/api/v1/alarms#fragment")]
    [InlineData("/api/v1/al arms")]
    public void A_malformed_prefix_is_refused_at_build_time(string prefix)
    {
        var exception = Assert.Throws<RouteOwnershipException>(() => RouteOwnershipTable.Build(
            [new RouteOwnership { PathPrefix = prefix, Owner = RouteOwner.Python }]));

        Assert.Contains("prefix", exception.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void A_prefix_declared_twice_in_configuration_is_refused()
    {
        var exception = Assert.Throws<RouteOwnershipException>(() => RouteOwnershipTable.Build(
            RouteOwnershipDefaults.Manifest,
            [
                new RouteOwnership { PathPrefix = "/api/v1/alarms", Owner = RouteOwner.Python },
                new RouteOwnership { PathPrefix = "/api/v1/alarms/", Owner = RouteOwner.Dotnet },
            ]));

        Assert.Contains("more than once", exception.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void An_empty_manifest_is_refused_rather_than_proxying_nothing()
    {
        var exception = Assert.Throws<RouteOwnershipException>(() =>
            RouteOwnershipTable.Build(RouteOwnershipDefaults.Manifest, [], replaceDefaults: true));

        Assert.Contains("empty", exception.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Trailing_slashes_are_normalised_away()
    {
        var table = RouteOwnershipTable.Build(
            [new RouteOwnership { PathPrefix = "/api/v1/energy/", Owner = RouteOwner.Python }]);

        Assert.Equal("/api/v1/energy", table.Entries.Single().PathPrefix);
        Assert.NotNull(table.Match("/api/v1/energy/state"));
    }

    [Fact]
    public void Rows_bind_from_configuration_in_the_shape_appsettings_uses()
    {
        // Proves the documented appsettings / CHAOS_ environment shape actually
        // binds - the manifest is worthless if it cannot be overridden on a node.
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["Chaos:Routes:0:PathPrefix"] = "/api/v1/alarms/definitions",
                ["Chaos:Routes:0:Owner"] = "Dotnet",
                ["Chaos:Routes:0:PortedInVersion"] = "0.6.0",
                ["Chaos:Routes:0:Notes"] = "ported",
            })
            .Build();

        var options = new Configuration.ChaosHostOptions();
        configuration.GetSection("Chaos").Bind(options);

        var route = Assert.Single(options.Routes);
        Assert.Equal("/api/v1/alarms/definitions", route.PathPrefix);
        Assert.Equal(RouteOwner.Dotnet, route.Owner);
        Assert.Equal("0.6.0", route.PortedInVersion);
        Assert.Equal("ported", route.Notes);
    }
}
