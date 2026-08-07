using Chaos.Host.Routing;
using Chaos.Host.Tests.Support;

namespace Chaos.Host.Tests;

/// <summary>
/// End-to-end proof that a bad manifest stops the host, rather than producing a
/// gateway that quietly 404s a subsystem.
/// </summary>
public sealed class StartupValidationTests
{
    [Fact]
    public void The_host_refuses_to_start_when_a_dotnet_route_has_no_endpoint()
    {
        using var factory = ChaosHostFactory.For()
            .WithRoute("/api/v1/alarms/definitions", RouteOwner.Dotnet, portedInVersion: "0.6.0")
            .Build();

        var thrown = Record.Exception(() => factory.CreateClient());

        Assert.NotNull(thrown);
        var ownership = thrown.Find<RouteOwnershipException>();
        Assert.True(
            ownership is not null,
            $"Expected a RouteOwnershipException in the chain, got: {thrown}");
        Assert.Contains("/api/v1/alarms/definitions", ownership!.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void The_host_refuses_to_start_on_a_malformed_manifest_row()
    {
        using var factory = ChaosHostFactory.For()
            .WithRoute("api/v1/alarms", RouteOwner.Python)
            .Build();

        var thrown = Record.Exception(() => factory.CreateClient());

        Assert.NotNull(thrown);
        Assert.NotNull(thrown.Find<RouteOwnershipException>());
    }

    [Fact]
    public void The_host_refuses_to_start_when_the_backend_is_not_on_loopback()
    {
        using var factory = ChaosHostFactory.For()
            .With("BackendUrl", "http://192.0.2.10:8081")
            .Build();

        var thrown = Record.Exception(() => factory.CreateClient());

        Assert.NotNull(thrown);
        Assert.Contains("loopback", thrown.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task A_non_loopback_backend_is_allowed_when_explicitly_opted_into()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend)
            .With("AllowNonLoopbackBackend", "true")
            .Build();

        using var client = factory.CreateClient();
        using var response = await client.GetAsync("/health/live");

        Assert.Equal(System.Net.HttpStatusCode.OK, response.StatusCode);
    }

    [Fact]
    public async Task The_host_starts_with_the_shipped_manifest()
    {
        await using var backend = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(backend).Build();

        using var client = factory.CreateClient();
        using var response = await client.GetAsync("/host/routes");

        Assert.Equal(System.Net.HttpStatusCode.OK, response.StatusCode);
    }
}
