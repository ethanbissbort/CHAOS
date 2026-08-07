using System.Net;
using System.Text.Json;
using Chaos.Host.Routing;
using Chaos.Host.Tests.Support;

namespace Chaos.Host.Tests;

/// <summary>
/// <c>/host/routes</c> and <c>/host/info</c> — the runtime view of the migration.
/// </summary>
public sealed class HostEndpointsTests
{
    [Fact]
    public async Task Host_routes_reflects_the_live_table()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend).Build();
        using var client = factory.CreateClient();

        using var response = await client.GetAsync("/host/routes");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        var body = await response.ReadJsonAsync();
        var rows = body.GetProperty("routes").EnumerateArray().ToList();

        Assert.Equal(RouteOwnershipDefaults.Manifest.Count, rows.Count);

        var alarms = rows.Single(row => row.GetStringProperty("pathPrefix") == "/api/v1/alarms");
        Assert.Equal("Python", alarms.GetStringProperty("owner"));
        Assert.Equal("Default", alarms.GetStringProperty("source"));
        Assert.Equal(backend.Url, alarms.GetStringProperty("target"));
        Assert.Equal(JsonValueKind.Null, alarms.GetProperty("portedInVersion").ValueKind);

        var host = rows.Single(row => row.GetStringProperty("pathPrefix") == "/host");
        Assert.Equal("Dotnet", host.GetStringProperty("owner"));
        Assert.Equal("Chaos.Host (in process)", host.GetStringProperty("target"));

        // A .NET-owned row names the endpoints that actually serve it, so
        // "ported" is a claim backed by something inspectable.
        var hostEndpoints = host.GetProperty("nativeEndpoints").EnumerateArray()
            .Select(element => element.GetString())
            .ToList();
        Assert.Contains("/host/info", hostEndpoints);
        Assert.Contains("/host/routes", hostEndpoints);

        var summary = body.GetProperty("summary");
        Assert.Equal(RouteOwnershipDefaults.Manifest.Count, summary.GetProperty("total").GetInt32());
        Assert.Equal(2, summary.GetProperty("dotnet").GetInt32());
        Assert.Equal(rows.Count - 2, summary.GetProperty("python").GetInt32());
    }

    [Fact]
    public async Task Host_routes_shows_a_configuration_override_as_such()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend)
            .WithRoute("/api/v1/alarms", RouteOwner.Python, notes: "pinned on this node")
            .Build();
        using var client = factory.CreateClient();

        var body = await (await client.GetAsync("/host/routes")).ReadJsonAsync();
        var alarms = body.GetProperty("routes").EnumerateArray()
            .Single(row => row.GetStringProperty("pathPrefix") == "/api/v1/alarms");

        Assert.Equal("ConfigurationOverride", alarms.GetStringProperty("source"));
        Assert.Equal("pinned on this node", alarms.GetStringProperty("notes"));
    }

    [Fact]
    public async Task Host_info_reports_configuration_platform_contract_and_migration_summary()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend).Build();
        using var client = factory.CreateClient();

        var body = await (await client.GetAsync("/host/info")).ReadJsonAsync();

        Assert.Equal("Project CHAOS", body.GetStringProperty("product"));
        Assert.Equal("Chaos.Host", body.GetStringProperty("component"));
        Assert.Equal(backend.Url, body.GetStringProperty("backendUrl"));
        Assert.Equal("0.4.0", body.GetStringProperty("platformVersionContract"));
        Assert.False(string.IsNullOrWhiteSpace(body.GetStringProperty("version")));

        var routes = body.GetProperty("routes");
        Assert.Equal(RouteOwnershipDefaults.Manifest.Count, routes.GetProperty("total").GetInt32());

        // Off Windows this must say so rather than guess.
        var windowsService = body.GetProperty("windowsService");
        Assert.False(windowsService.GetProperty("running").GetBoolean());
        Assert.Equal(OperatingSystem.IsWindows(), windowsService.GetProperty("supported").GetBoolean());
    }

    [Fact]
    public async Task Host_info_reports_that_no_supervisor_is_registered()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend).Build();
        using var client = factory.CreateClient();

        var body = await (await client.GetAsync("/host/info")).ReadJsonAsync();
        var supervisor = body.GetProperty("supervisor");

        Assert.False(supervisor.GetProperty("registered").GetBoolean());
        Assert.Equal(JsonValueKind.Null, supervisor.GetProperty("implementation").ValueKind);

        // Unknown, not "Stopped" and not "Running": the no-op supervisor has no
        // idea what the backend process is doing and says so.
        Assert.Equal("Unknown", supervisor.GetStringProperty("state"));
    }

    [Fact]
    public async Task Responses_are_labelled_with_the_component_that_served_them()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend).Build();
        using var client = factory.CreateClient();

        using var native = await client.GetAsync("/host/info");
        Assert.Equal("dotnet", native.Headers.GetValues("X-Chaos-Route-Owner").Single());
        Assert.Equal("/host", native.Headers.GetValues("X-Chaos-Route-Prefix").Single());

        using var proxied = await client.GetAsync("/api/v1/alarms/active");
        Assert.Equal("python", proxied.Headers.GetValues("X-Chaos-Route-Owner").Single());
        Assert.Equal("/api/v1/alarms", proxied.Headers.GetValues("X-Chaos-Route-Prefix").Single());
    }
}
